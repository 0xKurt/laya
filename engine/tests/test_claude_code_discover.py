# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Discovery parsing and the `claude -p` argument building of the Claude Code connector."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from laya.claude_code import cli, discover

@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    """No test of the connector may spawn `claude` or `gh`."""
    from laya.claude_code import cli as _cli

    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(_cli, "_run_process", side_effect=_refuse), \
         patch.object(_cli, "read_init", side_effect=_refuse), \
         patch.object(_cli, "cli_version", side_effect=_refuse), \
         patch.object(_cli, "run_gh", side_effect=_refuse):
        yield


SLACK = "mcp__claude_ai_Slack__"
TOOLS = [
    f"{SLACK}slack_search_public_and_private", f"{SLACK}slack_read_thread",
    f"{SLACK}slack_read_channel", f"{SLACK}slack_read_user_profile", f"{SLACK}slack_send_message",
    # A second Slack server with fewer tools must not win.
    "mcp__plugin_engineering_slack__slack_read_thread",
    "mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Google_Drive__read_file_content",
    "mcp__linear-server__get_notifications", "mcp__linear-server__list_issues",
    "mcp__linear-server__get_issue", "mcp__linear-server__whoami", "mcp__linear-server__save_issue",
]
SERVERS = [
    {"name": "claude.ai Slack", "status": "connected"},
    {"name": "claude.ai Gmail", "status": "connected"},
    {"name": "linear-server", "status": "connected"},
    {"name": "notion", "status": "needs-auth"},
]


def test_split_and_normalize():
    assert discover.split_tool("mcp__linear-server__get_issue") == ("linear-server", "get_issue")
    assert discover.split_tool("mcp__notion__notion-search") == ("notion", "notion-search")
    assert discover.split_tool("Bash") is None
    assert discover.normalize_server_name("claude.ai Slack") == "claude_ai_Slack"
    assert discover.normalize_server_name("linear-server") == "linear-server"


def test_map_tools_finds_servers_dynamically():
    result = discover.map_tools(TOOLS, SERVERS)
    slack = result["slack"]
    assert slack.available and slack.server == "claude_ai_Slack"
    assert slack.tools["search"] == f"{SLACK}slack_search_public_and_private"
    assert slack.tools["send"] == f"{SLACK}slack_send_message"
    gmail = result["gmail"]
    assert gmail.available and gmail.tools["draft"] == "mcp__claude_ai_Gmail__create_draft"
    assert gmail.tools["drive_read"] == "mcp__claude_ai_Google_Drive__read_file_content"
    linear = result["linear"]
    assert linear.available and linear.tools["whoami"] == "mcp__linear-server__whoami"
    notion = result["notion"]
    assert not notion.available and "needs-auth" in notion.problem


def test_map_tools_other_prefix_and_missing():
    tools = ["mcp__my_slack__slack_search_public", "mcp__my_slack__slack_read_thread"]
    result = discover.map_tools(tools, [{"name": "my slack", "status": "connected"}])
    assert not result["slack"].available
    assert "read_channel" in result["slack"].problem
    assert "no Gmail connector" in result["gmail"].problem
    tools.append("mcp__my_slack__slack_read_channel")
    result = discover.map_tools(tools, [])
    assert result["slack"].available
    assert result["slack"].tools["search"] == "mcp__my_slack__slack_search_public"


def test_read_roles_never_include_send_tools():
    result = discover.map_tools(TOOLS, SERVERS)
    for platform, roles in discover.READ_ROLES.items():
        assert discover.SEND_ROLES.get(platform) not in roles
    from laya.claude_code.platforms.common import read_tools
    tools = read_tools({"tools": result["slack"].tools}, "slack")
    assert f"{SLACK}slack_send_message" not in tools and tools


def test_identity_prompt_and_answer():
    result = discover.map_tools(TOOLS, SERVERS)
    prompt, tools = discover.identity_prompt(result)
    assert f"{SLACK}slack_read_user_profile" in tools and "mcp__linear-server__whoami" in tools
    assert "mcp__claude_ai_Gmail__send_message" not in tools
    assert "in:sent" in prompt
    ident = discover.identity_from_answer({
        "slack": {"user_id": "U1", "name": "Kim", "email": "Kim@X.com"},
        "gmail": {"email": "Kim@X.com", "name": "Kim"},
        "linear": {"id": "l1", "email": "kim@x.com", "name": "Kim"},
        "notion": None,
    })
    assert ident["slack"] == {"slack_user_id": "U1", "name": "Kim", "email": "kim@x.com"}
    assert ident["gmail"]["email"] == "kim@x.com"
    assert ident["linear"]["linear_user_id"] == "l1"
    assert "notion" not in ident


def test_build_args_allowlist_and_deny_list():
    args = cli.build_args(
        "claude", "prompt", tools=["mcp__a__read"], model="haiku", schema={"type": "object"},
        deny=["mcp__a__read", "mcp__a__send", "mcp__b__write"],
    )
    assert args[:3] == ["claude", "-p", "prompt"]
    assert args[args.index("--allowedTools") + 1] == "mcp__a__read"
    assert args[args.index("--disallowedTools") + 1] == "mcp__a__send,mcp__b__write"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert json.loads(args[args.index("--json-schema") + 1]) == {"type": "object"}


def test_weighted_input_tokens():
    assert cli.weighted_input_tokens(
        {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100}
    ) == 25


@pytest.mark.asyncio
async def test_run_collect_logs_audit_and_returns_structured_output(db):
    out = json.dumps({"is_error": False, "structured_output": {"items": []},
                      "usage": {"input_tokens": 3, "output_tokens": 4}})
    with patch.object(cli, "_run_process", new_callable=AsyncMock, return_value=[out]), \
         patch.object(cli, "get_agent_binary", return_value="claude"):
        result = await cli.run_collect("linear", "p", {"type": "object"}, ["mcp__x__y"], model="haiku")
    assert result == {"items": []}
    rows = await db.execute_fetchall("SELECT step, model_used, input_tokens, success FROM audit_log")
    assert dict(rows[0]) == {"step": "claude_code_linear", "model_used": "agent/claude_code/haiku",
                             "input_tokens": 3, "success": 1}


@pytest.mark.asyncio
async def test_run_collect_error(db):
    out = json.dumps({"is_error": True, "result": "MCP server failed", "usage": {}})
    with patch.object(cli, "_run_process", new_callable=AsyncMock, return_value=[out]), \
         patch.object(cli, "get_agent_binary", return_value="claude"):
        with pytest.raises(cli.ClaudeCodeError, match="MCP server failed"):
            await cli.run_collect("linear", "p", {}, ["t"])
    with pytest.raises(cli.ClaudeCodeError):
        await cli.run_collect("linear", "p", {}, [])
    rows = await db.execute_fetchall("SELECT success FROM audit_log")
    assert [r["success"] for r in rows] == [0]


@pytest.mark.asyncio
async def test_discover_end_to_end_with_mocks(db):
    discover.reset_cache()
    init = {"type": "system", "subtype": "init", "tools": ["Bash", *TOOLS], "mcp_servers": SERVERS}
    answer = {"slack": {"user_id": "U1", "name": "Kim", "email": "kim@x.com"},
              "gmail": {"email": "kim@x.com"}, "linear": {"id": "l", "email": "kim@x.com"}}
    with patch.object(discover.cli, "cli_version", new_callable=AsyncMock, return_value="2.1 (Claude Code)"), \
         patch.object(discover.cli, "read_init", new_callable=AsyncMock, return_value=init), \
         patch.object(discover.cli, "run_collect", new_callable=AsyncMock, return_value=answer) as turn, \
         patch.object(discover.cli, "run_gh", new_callable=AsyncMock, return_value={"login": "kim-gh"}), \
         patch.object(discover, "get_agent_binary", return_value="/bin/claude"):
        found = await discover.discover()
        again = await discover.discover()
    assert again is found and turn.await_count == 1
    assert found.cli == {"found": True, "path": "/bin/claude", "version": "2.1 (Claude Code)"}
    assert "Bash" not in found.all_tools
    assert found.platforms["slack"].identity["slack_user_id"] == "U1"
    assert found.platforms["github"].identity["github_login"] == "kim-gh"
    assert not found.platforms["notion"].available
    # The identity turn denies every other discovered tool.
    assert "mcp__claude_ai_Gmail__send_message" in turn.await_args.kwargs["deny"]
    assert list(found.platforms) == ["slack", "gmail", "linear", "github", "notion"]
    discover.reset_cache()


@pytest.mark.asyncio
async def test_discover_without_cli(db):
    discover.reset_cache()
    with patch.object(discover.cli, "cli_version", new_callable=AsyncMock, return_value=None), \
         patch.object(discover.cli, "run_gh", new_callable=AsyncMock,
                      side_effect=cli.ClaudeCodeError("gh is not installed")), \
         patch.object(discover, "get_agent_binary", return_value="claude"):
        found = await discover.discover(force=True)
    assert found.cli["found"] is False
    assert found.platforms["slack"].problem == "Claude Code CLI not found."
    assert found.platforms["github"].problem == "The GitHub CLI is not installed."
    discover.reset_cache()
