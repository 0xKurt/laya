# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Automatic (`agent/auto`, llm/agent_backend.py): the agent choice across logins of
several coding agents, failover to the next agent, streaming roles on streaming agents
only, the opt-in API fallback, the budget pause when every login is exhausted, and the
"what runs where" endpoint."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import accounts
from laya.llm import agent_backend as ab
from laya.pipeline import agent_budget

OK = {"five_hour": {"utilization": 0.1, "resets_at": None, "status": "allowed"}}
FULL = {"seven_day": {"utilization": 1.0, "resets_at": int(time.time()) + 3600, "status": "rejected"}}


@pytest.fixture
def agents(monkeypatch):
    """Both CLIs installed; each agent's login state set by the test."""
    state: dict[str, tuple[str | None, dict]] = {"claude_code": ("acct1", OK), "codex_cli": ("c1", OK)}
    monkeypatch.setattr(ab, "detect_agent_paths", lambda: {"claude_code": "/bin/claude", "codex_cli": "/bin/codex"})

    async def agent_state(agent_id):
        return state[agent_id]

    monkeypatch.setattr(ab, "_agent_state", agent_state)
    monkeypatch.setattr(ab, "_auto_routes", {})
    monkeypatch.setattr(accounts, "_thresholds", lambda: {"five_hour": 0.9, "seven_day": 0.8})
    return state


@pytest.mark.asyncio
async def test_prefers_native_order_then_skips_a_limited_agent(agents):
    assert await ab.choose_auto_agent() == "claude_code"
    agents["claude_code"] = ("acct1", FULL)
    assert await ab.choose_auto_agent() == "codex_cli"
    agents["codex_cli"] = ("c1", FULL)
    assert await ab.choose_auto_agent() is None


@pytest.mark.asyncio
async def test_codex_at_its_limit_is_skipped(agents):
    agents["codex_cli"] = ("c1", FULL)
    assert await ab.choose_auto_agent() == "claude_code"
    assert await ab.choose_auto_agent(exclude=("claude_code",)) is None


@pytest.mark.asyncio
async def test_streaming_roles_only_on_streaming_agents(agents):
    agents["claude_code"] = (None, {})
    assert await ab.choose_auto_agent(streaming=False) == "codex_cli"
    assert await ab.choose_auto_agent(streaming=True) is None


def test_role_models_per_agent():
    assert ab.auto_model_for("claude_code", "router") == "agent/claude_code/haiku"
    assert ab.auto_model_for("claude_code", "stager") == "agent/claude_code/sonnet"
    assert ab.auto_model_for("codex_cli", "router") == "agent/codex_cli"


@pytest.mark.asyncio
async def test_call_fails_over_to_the_next_agent(agents, monkeypatch, db):
    ran: list[str] = []

    async def routed(agent_id, *args):
        ran.append(agent_id)
        if agent_id == "claude_code":
            raise ab.AgentRateLimited("Agent 'claude_code' error: You've hit your limit")
        return {"content": '{"a": "x"}', "parsed": {"a": "x"}, "input_tokens": 1, "output_tokens": 1,
                "finish_reason": "stop", "effective_model": "", "cost_usd": None, "account_id": "c1"}

    monkeypatch.setattr(ab, "_run_agent_routed", routed)
    monkeypatch.setattr(ab, "get_agent_binary", lambda a: a)
    monkeypatch.setattr(agent_budget, "enforce_daily_call_limit", AsyncMock())
    schema = {"schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}}
    r = await ab.agent_llm_call("agent/auto/router", [{"role": "user", "content": "x"}], response_schema=schema)
    assert ran == ["claude_code", "codex_cli"]
    assert r.parsed == {"a": "x"} and r.resolved_model == "agent/codex_cli" and r.account_id == "c1"


@pytest.mark.asyncio
async def test_every_login_over_pauses_the_budget(agents, monkeypatch, db):
    agents["claude_code"] = ("acct1", FULL)
    agents["codex_cli"] = ("c1", FULL)
    until = "2026-11-21 10:14:00"
    monkeypatch.setattr(accounts, "native_block", AsyncMock(return_value=(True, until, "claude over")))
    from laya.claude_code import codex_accounts

    monkeypatch.setattr(codex_accounts, "native_block", AsyncMock(return_value=(True, until, "codex over")))
    pause = AsyncMock()
    monkeypatch.setattr(agent_budget, "pause_for_agent_budget", pause)
    monkeypatch.setattr(agent_budget, "enforce_daily_call_limit", AsyncMock())
    with pytest.raises(agent_budget.AgentBudgetRefused):
        await ab.agent_llm_call("agent/auto/stager", [{"role": "user", "content": "x"}])
    assert pause.await_args.args == (until, "claude over; codex over")


@pytest.mark.asyncio
async def test_api_fallback_only_when_allowed(agents, monkeypatch):
    agents["claude_code"] = ("acct1", FULL)
    agents["codex_cli"] = ("c1", FULL)
    settings = {"model_access": {"api_fallback": False}}
    monkeypatch.setattr("laya.config.load_settings", lambda: settings)
    monkeypatch.setattr("laya.security.keychain.has_api_key", lambda p: p == "openai")
    assert await ab.expand_auto("agent/auto", "router") == "agent/auto/router"
    settings["model_access"]["api_fallback"] = True
    monkeypatch.setattr("laya.pipeline.budget.is_budget_paused", AsyncMock(return_value=False))
    assert await ab.expand_auto("agent/auto", "router") == "gpt-4o-mini"
    assert await ab.expand_auto("agent/auto", "stager") == "gpt-4o"
    # The $ budget wins over the fallback.
    monkeypatch.setattr("laya.pipeline.budget.is_budget_paused", AsyncMock(return_value=True))
    assert await ab.expand_auto("agent/auto", "router") == "agent/auto/router"
    # Other models are untouched.
    assert await ab.expand_auto("agent/claude_code/haiku", "router") == "agent/claude_code/haiku"


@pytest.mark.asyncio
async def test_stream_on_auto_without_streaming_agent_is_an_error(agents, monkeypatch, db):
    agents["claude_code"] = (None, {})
    monkeypatch.setattr(accounts, "native_block", AsyncMock(return_value=(False, None, "")))
    events = [e async for e in ab.agent_llm_stream("agent/auto/chat", [{"role": "user", "content": "hi"}])]
    assert events[-1].type == "error" and "streaming agent" in events[-1].content


@pytest.mark.asyncio
async def test_auto_endpoint_reports_what_each_role_runs_on(agents, monkeypatch, db):
    from laya.main import app

    agents["claude_code"] = ("acct1", FULL)
    models = {"router": "agent/auto", "stager": "agent/auto", "omni": "agent/auto", "chat": "agent/auto",
              "trace": "claude-sonnet-4-6"}
    monkeypatch.setattr("laya.config.load_settings", lambda: {"models": models})
    monkeypatch.setattr(accounts, "account_label", AsyncMock(return_value="a@example.com"))
    from laya.claude_code import codex_accounts

    monkeypatch.setattr(codex_accounts, "label", AsyncMock(return_value="c@example.com"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        data = (await client.get("/claude-code/auto")).json()
    roles = data["roles"]
    assert roles["router"]["agent_id"] == "codex_cli" and roles["router"]["login_label"] == "c@example.com"
    assert roles["chat"]["via"] == "none" and "streaming" in roles["chat"]["reason"]
    assert roles["trace"] == {"automatic": False, "model": "claude-sonnet-4-6"}
