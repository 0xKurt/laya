# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Codex as an inference backend (llm/agent_backend.py, claude_code/codex_accounts.py):
the `codex exec` argv, native structured output through --output-schema (strict form),
parsing of captured JSONL, and usage-limit errors recorded with their reset time."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from laya.claude_code import codex_accounts
from laya.llm import agent_backend as ab
from laya.pipeline import agent_budget

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
SCHEMA = {
    "type": "object",
    "properties": {
        "persona": {"type": "string", "enum": ["engineer", "comms"]},
        "priority": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["persona", "priority"],
}


@pytest.fixture(autouse=True)
def _cli_login(monkeypatch):
    """The Codex CLI login, logged in, without starting `codex app-server`."""
    monkeypatch.setattr(codex_accounts, "_default_status", (time.monotonic(), {
        "logged_in": True, "email": "me@example.com", "plan": "pro", "account_id": "acct-1", "auth": "chatgpt"}))
    monkeypatch.setattr(codex_accounts, "_route", codex_accounts._Route())


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


# ── argv ─────────────────────────────────────────────────────────────────


def test_codex_exec_args_are_lean_and_noninteractive(tmp_path):
    args, env = ab._build_args(
        "codex_cli", "codex", "sys", "prompt", "gpt-5.4", {"schema": SCHEMA}, True,
        workdir=str(tmp_path),
    )
    assert args[:2] == ["codex", "exec"]
    # exec never asks for approval; the interactive-only flag must not be passed.
    assert "--ask-for-approval" not in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    for flag in ("--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config"):
        assert flag in args
    assert args[args.index("--model") + 1] == "gpt-5.4"
    schema_file = Path(args[args.index("--output-schema") + 1])
    assert schema_file.parent == tmp_path
    written = json.loads(schema_file.read_text())
    assert written["additionalProperties"] is False
    assert set(written["required"]) == {"persona", "priority", "note"}
    assert args[-1] == "sys\n\nprompt"
    assert env == {}


def test_codex_without_schema_has_no_schema_flag(tmp_path):
    args, _ = ab._build_args("codex_cli", "codex", "", "p", None, None, True, workdir=str(tmp_path))
    assert "--output-schema" not in args and "--model" not in args


def test_codex_tier_is_native():
    caps = {c["agent_id"]: c for c in ab.capabilities()}
    assert caps["codex_cli"]["tier"] == "native"


# ── strict schema ────────────────────────────────────────────────────────


def test_strict_schema_makes_optional_nullable_and_closes_objects():
    out = ab.strict_schema({
        "type": "object",
        "properties": {
            "a": {"type": "string", "default": "x"},
            "items": {"type": "array", "items": {"type": "object", "properties": {"b": {"type": "integer"}}}},
            "kind": {"type": "string", "enum": ["x", "y"]},
        },
        "required": ["a"],
    })
    assert out["required"] == ["a", "items", "kind"]
    assert "default" not in out["properties"]["a"]
    assert out["properties"]["items"]["type"] == ["array", "null"]
    inner = out["properties"]["items"]["items"]
    assert inner["additionalProperties"] is False and inner["properties"]["b"]["type"] == ["integer", "null"]
    assert out["properties"]["kind"]["enum"] == ["x", "y", None]


def test_strict_schema_refuses_free_form_objects():
    assert ab.strict_schema({"type": "object", "properties": {"meta": {"type": "object"}}}) is None
    assert ab.strict_schema({"type": "array", "items": {"type": "string"}}) is None


def test_relax_optional_drops_nulls_of_optional_fields_only():
    parsed = {"persona": "engineer", "priority": None, "note": None}
    assert ab.relax_optional(parsed, SCHEMA) == {"persona": "engineer", "priority": None}


# ── parsing captured output ──────────────────────────────────────────────


def test_parse_success_with_schema():
    out = ab._parse_output("codex_cli", _lines("exec_success_schema.jsonl"), native_schema=True)
    assert out["error"] is None
    assert out["parsed"] == {"persona": "engineer", "priority": "HIGH", "note": None}
    # 400 fresh + 2000 cached weighted at a tenth.
    assert out["input_tokens"] == 600 and out["output_tokens"] == 42


def test_parse_usage_limit_fixture():
    out = ab._parse_output("codex_cli", _lines("exec_usage_limit.jsonl"), native_schema=True)
    assert "usage limit" in out["error"]
    assert out["parsed"] is None


def test_error_event_followed_by_completion_is_not_a_failure():
    lines = [
        json.dumps({"type": "error", "message": "Reconnecting... 1/5"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]
    out = ab._parse_output("codex_cli", lines, native_schema=False)
    assert out["error"] is None and out["content"] == "hi"


# ── limit messages ───────────────────────────────────────────────────────


def test_parse_reset_clock_time_today_or_tomorrow():
    now = datetime(2026, 11, 20, 9, 0).timestamp()
    msg = "You’ve hit your usage limit. … or try again at 10:14 AM."
    assert datetime.fromtimestamp(codex_accounts.parse_reset(msg, now)) == datetime(2026, 11, 20, 10, 14)
    later = datetime(2026, 11, 20, 11, 0).timestamp()
    assert datetime.fromtimestamp(codex_accounts.parse_reset(msg, later)) == datetime(2026, 11, 21, 10, 14)
    pm = "try again at 3:04 PM"
    assert datetime.fromtimestamp(codex_accounts.parse_reset(pm, now)).hour == 15


def test_parse_reset_date_and_duration():
    now = datetime(2026, 11, 20, 9, 0).timestamp()
    dated = codex_accounts.parse_reset("try again at Nov 22nd, 2026 3:04 PM.", now)
    assert datetime.fromtimestamp(dated) == datetime(2026, 11, 22, 15, 4)
    dur = codex_accounts.parse_reset("try again in 2 days 3 hours 5 minutes.", now)
    assert dur == int(now) + 2 * 86400 + 3 * 3600 + 5 * 60


def test_limit_info_without_reset_holds():
    now = time.time()
    info = codex_accounts.limit_info("You've hit your usage limit.", now)
    assert info["status"] == "rejected"
    assert info["resetsAt"] == int(now) + codex_accounts.REJECTION_HOLD_SECONDS
    assert codex_accounts.limit_info("some other failure") is None


class _Proc:
    lines: list[str] = []

    def __init__(self):
        self.stderr_output = "Reading additional input from stdin..."

    async def spawn(self, args, cwd=None, env=None):
        self.args = args

    async def read_lines(self, idle_timeout=0):
        for line in _Proc.lines:
            yield line

    async def wait(self):
        return 1

    async def terminate(self):
        pass


@pytest.mark.asyncio
async def test_limit_error_is_recorded_and_raises(db, monkeypatch):
    _Proc.lines = _lines("exec_usage_limit.jsonl")
    monkeypatch.setattr(ab, "AgentProcess", _Proc)
    with pytest.raises(ab.AgentRateLimited):
        await ab._run_agent("codex_cli", "codex", "s", "u", None, {"schema": SCHEMA}, True, 30.0,
                            account_id="abc123")
    raw = await agent_budget.get_rate_limit_raw("codex_cli@abc123")
    assert raw["status"] == "rejected"
    reset = datetime.fromtimestamp(raw["resetsAt"])
    assert (reset.hour, reset.minute) == (10, 14)
    assert raw["resetsAt"] > time.time()
    windows = agent_budget.rate_limit_windows(raw)
    assert any(w["status"] == "rejected" for w in windows.values())


@pytest.mark.asyncio
async def test_agent_llm_call_parses_native_codex_reply(db, monkeypatch):
    _Proc.lines = _lines("exec_success_schema.jsonl")
    monkeypatch.setattr(ab, "AgentProcess", _Proc)
    monkeypatch.setattr(ab, "get_agent_binary", lambda agent_id: "codex")

    async def no_limit():
        return None

    monkeypatch.setattr(agent_budget, "enforce_daily_call_limit", no_limit)
    r = await ab.agent_llm_call("agent/codex_cli", [{"role": "user", "content": "x"}],
                                response_schema={"schema": SCHEMA})
    assert r.parsed == {"persona": "engineer", "priority": "HIGH"}


@pytest.mark.asyncio
async def test_refused_output_schema_falls_back_to_prompt_schema(db, monkeypatch):
    calls: list[bool] = []

    async def fake_run(agent_id, binary, system_text, user_prompt, model_string, schema, native, timeout, **kw):
        calls.append(native)
        if native:
            raise RuntimeError("Agent 'codex_cli' error: invalid_json_schema: bad output schema")
        assert "Schema:" in system_text
        return {"content": json.dumps({"persona": "comms", "priority": "LOW"}), "parsed": None,
                "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop",
                "effective_model": "", "cost_usd": None}

    async def no_limit():
        return None

    monkeypatch.setattr(ab, "_run_agent", fake_run)
    monkeypatch.setattr(ab, "_codex_schema_unsupported", False)
    monkeypatch.setattr(ab, "get_agent_binary", lambda agent_id: "codex")
    monkeypatch.setattr(agent_budget, "enforce_daily_call_limit", no_limit)
    r = await ab.agent_llm_call("agent/codex_cli", [{"role": "user", "content": "x"}],
                                response_schema={"schema": SCHEMA})
    assert calls == [True, False]
    assert r.parsed == {"persona": "comms", "priority": "LOW"}
    assert ab._codex_schema_unsupported is True
