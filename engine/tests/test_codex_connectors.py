# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Connector jobs on Codex logins (codex_accounts.Listing, accounts.pick_for_platform,
cli.run_collect): the MCP inventory mapped onto platform roles, the per-turn tool
restriction, the gating (unswitchable servers; sending and write actions stay on Claude
Code) and a collect turn against captured-shape JSONL."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from laya.claude_code import accounts, cli, codex_accounts

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _data(drop_dotted: bool = False) -> list[dict]:
    data = json.loads((FIXTURES / "app_server_mcp_status.json").read_text())["result"]["data"]
    return [s for s in data if not (drop_dotted and "." in s["name"])]


def test_listing_maps_linear_and_explains_notion():
    found = codex_accounts.Listing(_data())
    linear = found.platforms["linear"]
    assert linear.available and linear.tools["list_issues"] == "mcp__linear-server__list_issues"
    notion = found.platforms["notion"]
    assert not notion.available and "codex mcp login notion" in notion.problem
    assert "Codex has no Slack MCP server" in found.platforms["slack"].problem


def test_restrict_args_leave_only_the_turns_tools():
    found = codex_accounts.Listing(_data())
    args = found.restrict_args(["mcp__linear-server__list_issues", "mcp__linear-server__get_issue"])
    assert args[:2] == ["--disable", "apps"]
    assert "mcp_servers.linear-server.enabled_tools=[\"get_issue\", \"list_issues\"]" in args
    assert "mcp_servers.github.enabled=false" in args and "mcp_servers.notion.enabled=false" in args
    # A dotted name cannot be addressed by -c; it is reported instead.
    assert not any("justrunmy" in a for a in args)
    assert found.unswitchable({"linear-server"}) == ["justrunmy.app"]


@pytest.fixture
def codex_login(monkeypatch, db):
    """One added Codex login whose inventory is the fixture."""
    async def setup(drop_dotted: bool):
        home = codex_accounts.HOMES_DIR / "cx1"
        home.mkdir(parents=True, exist_ok=True)
        conn = await accounts._db()
        await conn.execute(
            "INSERT INTO claude_code_accounts (id, config_dir, email, logged_in, created_at, org_id, agent_id) "
            "VALUES ('cx1', ?, 'c@example.com', 1, '2026-11-20T09:00:00', 'acct-1', 'codex_cli')", (str(home),))
        await conn.commit()
        monkeypatch.setattr(codex_accounts, "_listings", {"cx1": codex_accounts.Listing(_data(drop_dotted))})
    monkeypatch.setattr("laya.config.detect_agent_paths", lambda: {"claude_code": "/c", "codex_cli": "/x"})
    # The Claude CLI login has no Linear.
    monkeypatch.setattr(accounts, "eligible_ids", AsyncMock(return_value=[accounts.DEFAULT_ID]))

    async def claude_listing(account_id, env, force=False):
        from laya.claude_code.discover import AccountListing, PlatformDiscovery
        return AccountListing(platforms={"linear": PlatformDiscovery(problem="no Linear")})

    monkeypatch.setattr("laya.claude_code.discover.account_listing", claude_listing)
    return setup


@pytest.mark.asyncio
async def test_read_jobs_go_to_codex_but_writes_stay_on_claude(codex_login):
    await codex_login(drop_dotted=True)
    assignment = await accounts.pick_for_platform("linear", ("list_issues",))
    assert assignment.agent_id == "codex_cli" and assignment.account_id == "cx1"
    assert assignment.env == {"CODEX_HOME": str(codex_accounts.HOMES_DIR / "cx1")}
    assert assignment.source_fields()["agent_id"] == "codex_cli"
    assert await accounts.pick_for_platform("linear", agents=accounts.READ_WRITE_AGENTS) is None


@pytest.mark.asyncio
async def test_login_with_unswitchable_server_is_not_used(codex_login):
    await codex_login(drop_dotted=False)
    assert await accounts.pick_for_platform("linear") is None
    reason = await accounts.platform_unavailable_reason("linear")
    assert "justrunmy.app" in reason and "c@example.com" in reason


class _Proc:
    lines: list[str] = []
    args: list[str] = []

    def __init__(self):
        self.stderr_output = ""

    async def spawn(self, args, cwd=None, env=None):
        _Proc.args = args
        _Proc.env = env

    async def read_lines(self, idle_timeout=0):
        for line in _Proc.lines:
            yield line

    async def wait(self):
        return 0

    async def terminate(self):
        pass


SCHEMA = {"type": "object", "properties": {"issues": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
    "required": ["id"]}}}, "required": ["issues"]}


@pytest.mark.asyncio
async def test_collect_turn_on_codex(codex_login, monkeypatch):
    await codex_login(drop_dotted=True)
    reply = {"issues": [{"id": "ENG-1", "title": None}]}
    _Proc.lines = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(reply)}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 9}}),
    ]
    monkeypatch.setattr("laya.agents.subprocess_helper.AgentProcess", _Proc)
    monkeypatch.setattr(cli, "get_agent_binary", lambda a: "codex")
    out = await cli.run_collect(
        "linear", "Call mcp__linear-server__list_issues.", SCHEMA, ["mcp__linear-server__list_issues"],
        env={"CODEX_HOME": "/h"}, account_id="cx1", agent_id="codex_cli",
    )
    assert out == {"issues": [{"id": "ENG-1"}]}
    args = _Proc.args
    assert args[:3] == ["codex", "exec", "--json"]
    assert "--ignore-user-config" not in args            # the login's own MCP servers are needed
    assert "mcp_servers.linear-server.enabled_tools=[\"list_issues\"]" in args
    assert "mcp_servers.github.enabled=false" in args and "--output-schema" in args
    assert _Proc.env == {"CODEX_HOME": "/h"}


@pytest.mark.asyncio
async def test_collect_limit_error_is_recorded(codex_login, monkeypatch):
    await codex_login(drop_dotted=True)
    _Proc.lines = (FIXTURES / "exec_usage_limit.jsonl").read_text().splitlines()
    monkeypatch.setattr("laya.agents.subprocess_helper.AgentProcess", _Proc)
    monkeypatch.setattr(cli, "get_agent_binary", lambda a: "codex")
    with pytest.raises(cli.ClaudeCodeError):
        await cli.run_collect("linear", "x", SCHEMA, ["mcp__linear-server__list_issues"],
                              account_id="cx1", agent_id="codex_cli")
    from laya.pipeline import agent_budget

    raw = await agent_budget.get_rate_limit_raw("codex_cli@cx1")
    assert raw["status"] == "rejected" and raw["resetsAt"] > time.time()
    assert "limit reached" in (await accounts.limit_reached("cx1")).lower()
