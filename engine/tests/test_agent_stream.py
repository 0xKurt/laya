# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for streaming chat / Coherence on the Claude Code agent backend.

The `claude -p` subprocess is replaced by a fake that replays recorded stream-json
lines, so these cover the stream parsing (partial messages, tool use, errors), the
argv (MCP config with a read-only turn token), the budget refusal, the audit +
rate-limit accounting, and llm_call's tool path on an agent model.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from laya.llm import agent_backend as ab
from laya.mcp import turn_tokens

MODEL = "agent/claude_code/haiku"


# ── recorded stream-json lines ───────────────────────────────────────────


def _se(event: dict) -> str:
    return json.dumps({"type": "stream_event", "event": event, "parent_tool_use_id": None})


def _text_turn(msg_id: str, *pieces: str) -> list[str]:
    lines = [_se({"type": "message_start", "message": {"id": msg_id}})]
    lines.append(_se({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}}))
    for p in pieces:
        lines.append(_se({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": p}}))
    lines.append(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {
        "id": msg_id, "content": [{"type": "text", "text": "".join(pieces)}]}}))
    return lines


def _tool_call(msg_id: str, tool_id: str, name: str, args: dict) -> list[str]:
    return [
        _se({"type": "message_start", "message": {"id": msg_id}}),
        _se({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": tool_id, "name": name, "input": {}}}),
        _se({"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}}),
        json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": msg_id,
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}]}}),
    ]


def _tool_result(tool_id: str, text: str, is_error: bool = False) -> str:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    if is_error:
        block["is_error"] = True
    return json.dumps({"type": "user", "parent_tool_use_id": None,
                       "message": {"role": "user", "content": [block]}})


RATE_LIMIT = {"status": "allowed", "resetsAt": 1795532400, "rateLimitType": "five_hour"}


def _result(text: str, *, is_error: bool = False) -> str:
    return json.dumps({
        "type": "result", "is_error": is_error, "result": text, "stop_reason": "end_turn",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 1000, "output_tokens": 7},
    })


# ── fake subprocess ──────────────────────────────────────────────────────


class FakeProcess:
    instances: list["FakeProcess"] = []
    lines: list[str] = []

    def __init__(self) -> None:
        self.args: list[str] = []
        self.config: dict | None = None
        self.token_allowlist = None
        self.terminated = False
        self.stderr_output = ""
        FakeProcess.instances.append(self)

    async def spawn(self, args, cwd=None, env=None):
        self.args = list(args)
        if "--mcp-config" in args:
            path = args[args.index("--mcp-config") + 1]
            with open(path) as f:
                self.config = json.load(f)
            auth = self.config["mcpServers"]["laya"]["headers"]["Authorization"]
            # What the MCP endpoint would grant this token while the turn runs.
            self.token_allowlist = turn_tokens.lookup_header(auth)

    async def read_lines(self, idle_timeout=300.0):
        for line in FakeProcess.lines:
            yield line

    async def wait(self):
        return 0

    async def terminate(self):
        self.terminated = True


@pytest.fixture
def fake(monkeypatch):
    FakeProcess.instances = []
    FakeProcess.lines = []
    audit = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(ab, "AgentProcess", FakeProcess)
    monkeypatch.setattr(ab, "get_agent_binary", lambda agent_id: "claude")
    monkeypatch.setattr("laya.pipeline.agent_budget.enforce_daily_call_limit", AsyncMock())
    monkeypatch.setattr("laya.pipeline.agent_budget.record_rate_limit", record)
    monkeypatch.setattr("laya.pipeline.queue.get_model_timeout", lambda: 30.0)
    monkeypatch.setattr("laya.llm.client.log_to_audit", audit)

    def _no_task(coro, **kw):
        coro.close()

    monkeypatch.setattr("laya.tasks.create_task", _no_task)
    return {"audit": audit, "record": record}


async def _collect(**kw):
    return [ev async for ev in ab.agent_llm_stream(MODEL, _messages(), **kw)]


def _messages():
    return [{"role": "system", "content": "You are Laya chat."},
            {"role": "user", "content": "What happened with PR 12?"}]


def _tool_defs(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


# ── parser ───────────────────────────────────────────────────────────────


def test_parser_emits_partial_text_once():
    p = ab.ClaudeStreamParser()
    items = [i for line in _text_turn("m1", "Hel", "lo") for i in p.feed(line)]
    assert items == [("text", "Hel"), ("text", "lo")]


def test_parser_falls_back_to_complete_message_without_partials():
    p = ab.ClaudeStreamParser()
    line = json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "text", "text": "whole answer"}]}})
    assert p.feed(line) == [("text", "whole answer")]


def test_parser_separates_text_of_successive_messages_and_reports_tools():
    p = ab.ClaudeStreamParser()
    lines = (
        _text_turn("m1", "Let me look.")
        + _tool_call("m2", "t1", "mcp__laya__search_cards", {"query": "PR 12"})
        + [_tool_result("t1", "card_abc: PR 12 merged")]
        + _text_turn("m3", "It was merged.")
    )
    items = [i for line in lines for i in p.feed(line)]
    assert items == [
        ("text", "Let me look."),
        ("tool", "calling", "t1", "search_cards", {"query": "PR 12"}),
        ("tool", "done", "t1", "search_cards", {"query": "PR 12"},
         "card_abc: PR 12 merged", False),
        ("text", "\n\nIt was merged."),
    ]
    assert p.tools_used == ["search_cards"]


def test_parser_ignores_thinking_and_junk_and_records_result():
    p = ab.ClaudeStreamParser()
    assert p.feed("not json") == []
    assert p.feed(_se({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "hmm"}})) == []
    assert p.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": RATE_LIMIT})) == []
    assert p.feed(_result("ok")) == []
    assert p.rate_limit_info == RATE_LIMIT
    assert p.result["result"] == "ok"


# ── argv / MCP config ────────────────────────────────────────────────────


def test_turn_mcp_config_points_at_laya_streamable_endpoint():
    from laya.config import ENGINE_HOST, ENGINE_PORT

    cfg = ab.build_turn_mcp_config("lyat_turn_x", "space 1")
    server = cfg["mcpServers"]["laya"]
    assert list(cfg["mcpServers"]) == ["laya"]
    assert server["type"] == "http"
    assert server["url"] == f"http://{ENGINE_HOST}:{ENGINE_PORT}/mcp/?space_id=space%201"
    assert server["headers"] == {"Authorization": "Bearer lyat_turn_x"}


def test_laya_read_tool_names_drops_write_and_egress_tools():
    from laya.llm.tools.definitions import egress_tool_names, write_tool_names

    write = sorted(write_tool_names())[0]
    egress = sorted(egress_tool_names())[0]
    names = ab.laya_read_tool_names(_tool_defs("search_cards", "get_card", write, egress))
    assert names == {"search_cards", "get_card"}


def test_stream_args_without_tools_keep_safe_mode_and_no_mcp():
    args = ab._claude_stream_args("claude", "sys", "prompt", "haiku")
    assert "--include-partial-messages" in args
    assert "--safe-mode" in args and "--strict-mcp-config" in args
    assert "--mcp-config" not in args and "--allowedTools" not in args


# ── streaming turn ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_yields_chunks_done_and_accounts(fake):
    FakeProcess.lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        *_text_turn("m1", "Hi ", "there"),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": RATE_LIMIT}),
        _result("Hi there"),
    ]
    events = await _collect(step="trace")
    assert [e.type for e in events] == ["chunk", "chunk", "done"]
    assert "".join(e.content for e in events if e.type == "chunk") == "Hi there"
    done = events[-1]
    assert done.input_tokens == 10 + 100 + 100
    assert done.output_tokens == 7
    assert done.model == MODEL

    proc = FakeProcess.instances[0]
    assert "--mcp-config" not in proc.args
    assert "--safe-mode" in proc.args
    assert proc.args[proc.args.index("--model") + 1] == "haiku"

    audit = fake["audit"].await_args.kwargs
    assert audit["model"] == MODEL and audit["step"] == "trace" and audit["success"] is True
    assert audit["metadata"]["backend"] == "agent"
    fake["record"].assert_awaited_once_with("claude_code", RATE_LIMIT)


@pytest.mark.asyncio
async def test_stream_tool_turn_uses_read_only_turn_token(fake):
    from laya.llm.tools.definitions import write_tool_names

    write = sorted(write_tool_names())[0]
    FakeProcess.lines = [
        *_tool_call("m1", "t1", "mcp__laya__search_cards", {"query": "PR 12"}),
        _tool_result("t1", "card_abc"),
        *_text_turn("m2", "Merged."),
        _result("Merged."),
    ]
    events = await _collect(tools=_tool_defs("search_cards", write), space_id="s1")

    tool_events = [(e.type, e.tool_status, e.tool_calls[0].name) for e in events
                   if e.type == "tool_status"]
    assert tool_events == [("tool_status", "calling", "search_cards"),
                           ("tool_status", "done", "search_cards")]
    assert not any(e.type == "tool_calls" for e in events)
    assert events[-1].type == "done"

    proc = FakeProcess.instances[0]
    assert "--safe-mode" not in proc.args
    assert proc.args[proc.args.index("--tools") + 1] == ""
    allowed = [proc.args[i + 1] for i, a in enumerate(proc.args) if a == "--allowedTools"]
    assert allowed == ["mcp__laya__search_cards"]
    assert proc.config["mcpServers"]["laya"]["url"].endswith("/mcp/?space_id=s1")
    # The token granted only the read tool while the turn ran, and is revoked after.
    assert proc.token_allowlist == frozenset({"search_cards"})
    auth = proc.config["mcpServers"]["laya"]["headers"]["Authorization"]
    assert turn_tokens.lookup_header(auth) is None
    assert fake["audit"].await_args.kwargs["metadata"]["agent_tools"] == ["search_cards"]


@pytest.mark.asyncio
async def test_stream_result_error_yields_error_and_audits_failure(fake):
    FakeProcess.lines = [_result("You've hit your limit", is_error=True)]
    events = await _collect()
    assert [e.type for e in events] == ["error"]
    assert "hit your limit" in events[0].content
    assert fake["audit"].await_args.kwargs["success"] is False
    fake["record"].assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_without_result_is_an_error(fake):
    FakeProcess.lines = _text_turn("m1", "partial")
    events = await _collect()
    assert [e.type for e in events] == ["chunk", "error"]
    assert "without a result" in events[-1].content


@pytest.mark.asyncio
async def test_stream_budget_refusal_never_spawns(fake, monkeypatch):
    from laya.pipeline.agent_budget import AgentCallLimitReached

    monkeypatch.setattr(
        "laya.pipeline.agent_budget.enforce_daily_call_limit",
        AsyncMock(side_effect=AgentCallLimitReached("daily agent call limit reached (5/5)")),
    )
    events = await _collect()
    assert [e.type for e in events] == ["error"]
    assert "daily agent call limit" in events[0].content
    assert FakeProcess.instances == []
    fake["audit"].assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_rejects_non_claude_agents(fake):
    events = [ev async for ev in ab.agent_llm_stream("agent/codex_cli/gpt", _messages())]
    assert [e.type for e in events] == ["error"]
    assert FakeProcess.instances == []


# ── client.py dispatch ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_call_streaming_dispatches_agent_models(fake):
    from laya.llm import client

    FakeProcess.lines = [*_text_turn("m1", "ok"), _result("ok")]
    with patch.object(client, "_get_model_for_role", return_value=MODEL):
        events = [e async for e in client.llm_call_streaming("chat", _messages())]
    assert [e.type for e in events] == ["chunk", "done"]


@pytest.mark.asyncio
async def test_llm_call_with_tools_on_agent_model_returns_final_text(fake):
    from laya.llm import client

    FakeProcess.lines = [
        *_tool_call("m1", "t1", "mcp__laya__search_cards", {"query": "x"}),
        _tool_result("t1", "card_abc"),
        *_text_turn("m2", "Found card_abc."),
        _result("Found card_abc."),
    ]
    with patch.object(client, "_get_model_for_role", return_value=MODEL):
        resp = await client.llm_call(
            "chat", _messages(), step="chat", tools=_tool_defs("search_cards"),
        )
    assert resp.content == "Found card_abc."
    assert resp.tool_calls is None
    assert resp.model == MODEL
    assert "--mcp-config" in FakeProcess.instances[0].args


@pytest.mark.asyncio
async def test_llm_call_with_tools_on_agent_model_raises_on_error(fake):
    from laya.llm import client

    FakeProcess.lines = [_result("boom", is_error=True)]
    with patch.object(client, "_get_model_for_role", return_value=MODEL):
        with pytest.raises(RuntimeError, match="boom"):
            await client.llm_call("chat", _messages(), tools=_tool_defs("search_cards"))
