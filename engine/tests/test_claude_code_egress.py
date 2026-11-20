# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Sending through Claude Code: the send connection hooks, routing, the
one-tool allowlist per action, payloads, result validation and the
health / test / remove delegation. No test spawns `claude`."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from laya.claude_code import cli, connect, discover, egress, store
from laya.claude_code.discover import Discovery, PlatformDiscovery
from laya.db.timeutil import db_now
from laya.egress import connections, health
from laya.egress import router as egress_router
from laya.egress import tool_handlers
from laya.egress.models import EgressRequest

SLACK_SEND = "mcp__claude_ai_Slack__slack_send_message"
SLACK_READ = {"search": "mcp__claude_ai_Slack__slack_search_public",
              "read_thread": "mcp__claude_ai_Slack__slack_read_thread",
              "read_channel": "mcp__claude_ai_Slack__slack_read_channel"}
GMAIL_DRAFT = "mcp__claude_ai_Gmail__create_draft"
GMAIL_SEND = "mcp__claude_ai_Gmail__send_message"
GMAIL_READ = {"search": "mcp__claude_ai_Gmail__search_threads",
              "get_thread": "mcp__claude_ai_Gmail__get_thread"}
ALL_TOOLS = [SLACK_SEND, *SLACK_READ.values(), GMAIL_DRAFT, GMAIL_SEND, *GMAIL_READ.values(),
             "mcp__claude_ai_Slack__slack_send_message_draft", "mcp__linear-server__save_issue"]


@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(cli, "_run_process", side_effect=_refuse), \
         patch.object(cli, "read_init", side_effect=_refuse), \
         patch.object(cli, "cli_version", side_effect=_refuse), \
         patch.object(cli, "run_gh", side_effect=_refuse):
        yield


def _pd(platform):
    if platform == "slack":
        return PlatformDiscovery(available=True, tools={**SLACK_READ, "send": SLACK_SEND},
                                 identity={"slack_user_id": "U_ME"})
    return PlatformDiscovery(available=True, tools={**GMAIL_READ, "draft": GMAIL_DRAFT},
                             identity={"email": "kim@work.com"})


@pytest.fixture
def found():
    d = Discovery(cli={"found": True}, platforms={"slack": _pd("slack"), "gmail": _pd("gmail")},
                  all_tools=list(ALL_TOOLS), at=1e12, identity_read=True)
    discover._cache = d
    yield d
    discover.reset_cache()


async def _connect(db, platform, space_id="default"):
    await db.execute("INSERT OR IGNORE INTO spaces (space_id, name) VALUES (?, ?)", (space_id, space_id))
    await db.execute(
        """INSERT OR REPLACE INTO sources (source_id, name, platform, workflow_id, space_id, source_type)
           VALUES (?, ?, ?, ?, ?, 'claude_code')""",
        (f"src_claude_code_{platform}", platform, platform, f"claude_code:{platform}", space_id),
    )
    await db.commit()
    await store.upsert_source(platform, tools=_pd(platform).tools, identity={}, enabled=True,
                              config=store.default_config(platform), connected_at=store.now_iso())
    await egress.on_connect(platform, _pd(platform))


async def _event(db, event_id, platform, metadata, *, conn=None, actor_email="", subject="Budget"):
    await db.execute(
        """INSERT INTO events (event_id, timestamp, source_platform, source_connection_id,
               source_raw_event_type, actor_email, subject_type, subject_id, subject_title,
               content_metadata, raw_json, processing_status)
           VALUES (?, ?, ?, ?, 'x', ?, 't', 's', ?, ?, '{}', 'completed')""",
        (event_id, db_now(), platform, conn or f"claude_code:{platform}", actor_email, subject,
         json.dumps(metadata)),
    )
    await db.commit()


async def _n8n_row(db, cid, platform, created="2020-01-01 00:00:00", space_id=None):
    await db.execute(
        """INSERT INTO egress_connections (connection_id, platform, name, n8n_credential_id, space_id,
               status, capabilities, created_at, updated_at)
           VALUES (?, ?, 'n8n', 'cred', ?, 'connected', '[]', ?, ?)""",
        (cid, platform, space_id, created, created),
    )
    await db.commit()


def _turn(output, **extra):
    return {"is_error": False, "structured_output": output, **extra}


# ── hooks ────────────────────────────────────────────────────────────────


def test_hooks_registered_once():
    egress.register_hooks()
    assert connect.CONNECT_HOOKS.count(egress.on_connect) == 1
    assert connect.DISCONNECT_HOOKS.count(egress.on_disconnect) == 1


@pytest.mark.asyncio
async def test_connect_creates_rows_with_contract_capabilities(db, found):
    await _connect(db, "slack")
    await _connect(db, "gmail", space_id="work")
    await egress.on_connect("github", PlatformDiscovery(available=True))
    rows = {r["connection_id"]: r for r in await db.execute_fetchall("SELECT * FROM egress_connections")}
    assert set(rows) == {"claude_code:slack", "claude_code:gmail"}
    slack, gmail = rows["claude_code:slack"], rows["claude_code:gmail"]
    assert slack["n8n_credential_id"] is None and slack["status"] == "connected"
    assert slack["name"] == "Slack (Claude Code)" and gmail["name"] == "Gmail (Claude Code)"
    assert json.loads(slack["capabilities"]) == ["reply_thread", "send_message"]
    assert json.loads(gmail["capabilities"]) == ["create_draft", "reply"]
    assert gmail["space_id"] == "work"

    # The Slack capabilities are action types of the Slack platform contract.
    from laya.egress.registry import get_capabilities
    contract = {c.action_type for c in get_capabilities("slack")}
    assert set(egress.CAPABILITIES["slack"]) <= contract


@pytest.mark.asyncio
async def test_connect_again_updates_and_status_follows_discovery(db, found):
    await _connect(db, "slack")
    pd = _pd("slack")
    pd.tools.pop("send")
    await egress.on_connect("slack", pd)
    rows = await db.execute_fetchall("SELECT * FROM egress_connections")
    assert len(rows) == 1
    assert rows[0]["status"] == "error" and "send tool" in rows[0]["error_message"]


@pytest.mark.asyncio
async def test_disconnect_removes_row(db, found):
    await _connect(db, "slack")
    await _connect(db, "gmail")
    await egress.on_disconnect("slack")
    await egress.on_disconnect("github")
    rows = await db.execute_fetchall("SELECT connection_id FROM egress_connections")
    assert [r["connection_id"] for r in rows] == ["claude_code:gmail"]


# ── routing ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_explicit_claude_code_connection(db, found):
    req = EgressRequest(platform="slack", action_type="send_message", payload={},
                        connection_id="claude_code:slack")
    assert await egress_router._resolve_backend(req) is egress.BACKEND


@pytest.mark.asyncio
async def test_router_event_collected_by_claude_code(db, found):
    await _connect(db, "slack")
    await _event(db, "evt_cc", "slack", {"slack_channel_id": "C1"})
    req = EgressRequest(platform="slack", action_type="reply_thread", payload={}, source_event_id="evt_cc")
    assert await egress_router._resolve_backend(req) is egress.BACKEND
    assert req.connection_id == "claude_code:slack"


@pytest.mark.asyncio
async def test_router_never_takes_n8n_requests(db, found):
    await _connect(db, "slack")
    await _event(db, "evt_n8n", "slack", {"slack_channel": "C1"}, conn="wf_123")
    await _event(db, "evt_cc", "slack", {"slack_channel_id": "C1"})
    n8n = egress_router._n8n_backend
    # n8n connection named explicitly, even on a Claude Code event.
    req = EgressRequest(platform="slack", action_type="send_message", payload={},
                        connection_id="conn_n8n", source_event_id="evt_cc")
    assert await egress_router._resolve_backend(req) is n8n
    # Event ingested by n8n, no connection.
    req = EgressRequest(platform="slack", action_type="send_message", payload={}, source_event_id="evt_n8n")
    assert await egress_router._resolve_backend(req) is n8n and req.connection_id is None
    # Claude Code event, but an action this backend does not perform.
    req = EgressRequest(platform="slack", action_type="react", payload={}, source_event_id="evt_cc")
    assert await egress_router._resolve_backend(req) is n8n
    # No event, no connection.
    req = EgressRequest(platform="slack", action_type="send_message", payload={})
    assert await egress_router._resolve_backend(req) is n8n


@pytest.mark.asyncio
async def test_router_event_without_send_connection_stays_n8n(db, found):
    await _event(db, "evt_cc", "slack", {"slack_channel_id": "C1"})
    req = EgressRequest(platform="slack", action_type="send_message", payload={}, source_event_id="evt_cc")
    assert await egress_router._resolve_backend(req) is egress_router._n8n_backend


@pytest.mark.asyncio
async def test_chat_resolution_prefers_other_connections(db, found):
    await _connect(db, "slack", space_id="work")
    assert await tool_handlers._resolve_connection_id("slack", "work") == "claude_code:slack"
    await _n8n_row(db, "conn_n8n", "slack", created="2999-01-01 00:00:00")
    assert await tool_handlers._resolve_connection_id("slack", "work") == "conn_n8n"
    assert await tool_handlers._resolve_connection_id("slack", None) == "conn_n8n"


# ── execute: allowlist and payload ───────────────────────────────────────


@pytest.mark.asyncio
async def test_slack_reply_uses_only_the_send_tool_and_verbatim_payload(db, found):
    await _connect(db, "slack")
    await _event(db, "evt_cc", "slack", {"slack_channel_id": "C42", "slack_thread_ts": "1700.1"})
    text = "Shipped — see *PR #12*.\n\n`0xAbC` stays as is; ünïcode too"
    run = AsyncMock(return_value=_turn({"ok": True, "permalink": "https://x.slack.com/p1", "ts": "1700.2", "error": ""}))
    with patch.object(cli, "run_send", run):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="slack", action_type="reply_thread", payload={"message": text},
            source_event_id="evt_cc"), {})
    assert res.success and res.result_url == "https://x.slack.com/p1"
    assert res.result_data["ts"] == "1700.2"
    (platform, prompt, tool), kw = run.call_args
    assert platform == "slack" and tool == SLACK_SEND
    assert kw["deny"] == sorted(set(ALL_TOOLS) - {SLACK_SEND})
    assert kw["schema"]["required"] == ["ok", "permalink", "ts", "error"]
    args = json.loads(prompt.split("ARGUMENTS (JSON):\n", 1)[1].split("\nIf a parameter", 1)[0])
    assert args == {"channel_id": "C42", "message": text, "thread_ts": "1700.1"}


@pytest.mark.asyncio
async def test_slack_allowlist_is_exactly_one_tool(db, found):
    await _connect(db, "slack")
    captured = {}

    async def fake_process(args, timeout, env=None):
        captured["args"] = args
        return [json.dumps(_turn({"ok": True, "permalink": "https://x.slack.com/p", "ts": "1", "error": ""}))]

    with patch.object(cli, "_run_process", side_effect=fake_process), \
         patch.object(cli, "get_agent_binary", return_value="/bin/claude"), \
         patch("laya.llm.client.log_to_audit", AsyncMock()):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="slack", action_type="send_message",
            payload={"channel": "#general", "message": "hi"}), {})
    assert res.success
    args = captured["args"]
    assert args[args.index("--allowedTools") + 1] == SLACK_SEND
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    denied = args[args.index("--disallowedTools") + 1].split(",")
    assert SLACK_SEND not in denied and GMAIL_SEND in denied and GMAIL_DRAFT in denied


@pytest.mark.asyncio
async def test_slack_send_message_has_no_thread(db, found):
    await _connect(db, "slack")
    await _event(db, "evt_cc", "slack", {"slack_channel_id": "C42", "slack_thread_ts": "1700.1"})
    run = AsyncMock(return_value=_turn({"ok": True, "permalink": "", "ts": "9.9", "error": ""}))
    with patch.object(cli, "run_send", run):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="slack", action_type="send_message", payload={"message": "top"},
            source_event_id="evt_cc"), {})
    assert res.success and res.result_url is None
    assert '"thread_ts"' not in run.call_args.args[1]


@pytest.mark.asyncio
async def test_gmail_creates_draft_never_sends(db, found):
    await _connect(db, "gmail")
    await _event(db, "evt_g", "gmail", {"gmail_thread_id": "T9"}, actor_email="ann@x.com",
                 subject="Re: Budget")
    body = "Hi Ann,\n\nnumbers attached.\n\n— Kim"
    run = AsyncMock(return_value=_turn({"ok": True, "draft_id": "r-1", "thread_id": "T9",
                                        "url": "https://mail.google.com/d", "error": ""}))
    with patch.object(cli, "run_send", run):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="gmail", action_type="send_email", payload={"body": body, "subject": "x"},
            source_event_id="evt_g"), {})
    assert res.success and res.result_data["draft"] is True and res.result_data["sent"] is False
    assert res.result_data["draft_id"] == "r-1"
    (_, prompt, tool), kw = run.call_args
    assert tool == GMAIL_DRAFT and "send" not in tool.lower()
    assert GMAIL_SEND in kw["deny"] and GMAIL_DRAFT not in kw["deny"]
    assert "DRAFT" in prompt
    args = json.loads(prompt.split("ARGUMENTS (JSON):\n", 1)[1].split("\nIf a parameter", 1)[0])
    assert args == {"to": ["ann@x.com"], "subject": "Re: Budget", "body": body, "threadId": "T9"}


@pytest.mark.asyncio
async def test_gmail_never_gets_a_send_tool_even_if_discovered_as_draft(db, found):
    await _connect(db, "gmail")
    found.platforms["gmail"].tools["draft"] = GMAIL_SEND
    run = AsyncMock()
    with patch.object(cli, "run_send", run):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="gmail", action_type="create_draft",
            payload={"to": "a@b.c", "subject": "s", "body": "b"}), {})
    assert not res.success and "send tool" in res.error
    run.assert_not_called()
    assert egress.send_tool("gmail", {"draft": "mcp__x__send_draft_create_draft"}) is None
    assert egress.send_tool("gmail", {"draft": GMAIL_DRAFT}) == GMAIL_DRAFT


def test_supports():
    b = egress.BACKEND
    assert b.supports("slack", "reply_thread") and b.supports("slack", "send_message")
    assert b.supports("gmail", "send_email") and b.supports("gmail", "create_draft")
    assert not b.supports("slack", "react") and not b.supports("gmail", "forward")
    assert not b.supports("gmail", "archive") and not b.supports("jira", "comment")


# ── execute: failure modes ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,action,err", [
    ({"message": "hi"}, "send_message", "Missing 'channel'"),
    ({"channel": "C1"}, "send_message", "Missing message text"),
    ({"channel": "C1", "message": "hi"}, "reply_thread", "thread_ts"),
])
async def test_slack_invalid_payload(db, found, payload, action, err):
    await _connect(db, "slack")
    run = AsyncMock()
    with patch.object(cli, "run_send", run):
        res = await egress.BACKEND.execute(EgressRequest(platform="slack", action_type=action, payload=payload), {})
    assert not res.success and err in res.error
    run.assert_not_called()


@pytest.mark.asyncio
async def test_gmail_missing_recipient(db, found):
    await _connect(db, "gmail")
    run = AsyncMock()
    with patch.object(cli, "run_send", run):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="gmail", action_type="create_draft", payload={"body": "b"}), {})
    assert not res.success and "'to'" in res.error
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("data,err", [
    (_turn({"ok": False, "permalink": "", "ts": "", "error": "channel_not_found"}), "channel_not_found"),
    (_turn({"ok": True, "permalink": "", "ts": "", "error": ""}), "no message link"),
    (_turn({"ok": True, "permalink": "javascript:x", "ts": "1", "error": ""}), "invalid message link"),
    (_turn(None), "no result"),
    (_turn({"ok": True, "permalink": "https://x", "ts": "1", "error": ""},
           permission_denials=[{"tool_name": "mcp__x__other"}]), "mcp__x__other"),
])
async def test_slack_bad_results(db, found, data, err):
    await _connect(db, "slack")
    with patch.object(cli, "run_send", AsyncMock(return_value=data)):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="slack", action_type="send_message", payload={"channel": "C1", "message": "m"}), {})
    assert not res.success and err in res.error and not res.retryable


@pytest.mark.asyncio
async def test_gmail_result_without_draft_id(db, found):
    await _connect(db, "gmail")
    data = _turn({"ok": True, "draft_id": "", "thread_id": "", "url": "", "error": ""})
    with patch.object(cli, "run_send", AsyncMock(return_value=data)):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="gmail", action_type="create_draft", payload={"to": "a@b.c", "body": "b"}), {})
    assert not res.success and "draft id" in res.error


@pytest.mark.asyncio
async def test_cli_error_is_reported_not_retryable(db, found):
    await _connect(db, "slack")
    with patch.object(cli, "run_send", AsyncMock(side_effect=cli.ClaudeCodeError("timed out"))):
        res = await egress.BACKEND.execute(EgressRequest(
            platform="slack", action_type="send_message", payload={"channel": "C1", "message": "m"}), {})
    assert not res.success and "timed out" in res.error and not res.retryable


@pytest.mark.asyncio
async def test_refuses_without_connection_or_when_unhealthy_or_mismatched(db, found):
    run = AsyncMock()
    with patch.object(cli, "run_send", run):
        req = dict(platform="slack", action_type="send_message", payload={"channel": "C1", "message": "m"})
        res = await egress.BACKEND.execute(EgressRequest(**req), {})
        assert not res.success and "not connected" in res.error
        await _connect(db, "slack")
        await db.execute("UPDATE egress_connections SET status = 'error', error_message = 'gone'")
        await db.commit()
        res = await egress.BACKEND.execute(EgressRequest(**req), {})
        assert not res.success and "gone" in res.error
        res = await egress.BACKEND.execute(EgressRequest(**req, connection_id="claude_code:gmail"), {})
        assert not res.success and "cannot send" in res.error
        res = await egress.BACKEND.execute(EgressRequest(
            platform="slack", action_type="react", payload={}), {})
        assert not res.success and "not available" in res.error
    run.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_does_not_execute(db, found):
    await _connect(db, "slack")
    run = AsyncMock()
    with patch.object(cli, "run_send", run):
        res = await egress_router.route_and_execute(EgressRequest(
            platform="slack", action_type="send_message", payload={"channel": "C1", "message": "m"},
            connection_id="claude_code:slack", dry_run=True))
    assert res.success and res.result_data == {"dry_run": True}
    run.assert_not_called()


# ── health / test / remove delegation ────────────────────────────────────


@pytest.mark.asyncio
async def test_health_monitor_uses_module_status(db, found):
    await _connect(db, "slack")
    found.platforms["slack"] = PlatformDiscovery(available=False, problem="Slack connector is 'needs-auth'")
    with patch.object(health, "_check_oauth_token", AsyncMock(side_effect=AssertionError)), \
         patch.object(health, "_get_from_keychain", side_effect=AssertionError):
        await health._run_health_checks()
    row = (await db.execute_fetchall("SELECT status, error_message FROM egress_connections"))[0]
    assert row["status"] == "error" and "needs-auth" in row["error_message"]


@pytest.mark.asyncio
async def test_health_gmail_row_skips_oauth(db, found):
    await _connect(db, "gmail")
    with patch.object(health, "_check_oauth_token", AsyncMock(side_effect=AssertionError)):
        await health._run_health_checks()
    row = (await db.execute_fetchall("SELECT status FROM egress_connections"))[0]
    assert row["status"] == "connected"


@pytest.mark.asyncio
async def test_test_connection_delegates_with_fresh_discovery(db, found):
    await _connect(db, "slack")
    fresh = AsyncMock(return_value=found)
    with patch.object(discover, "discover", fresh), \
         patch.object(connections, "_get_from_keychain", side_effect=AssertionError):
        assert await connections.test_connection("claude_code:slack") == (True, None)
    fresh.assert_awaited_once_with(force=True, with_identity=False)
    found.all_tools.remove(SLACK_SEND)
    with patch.object(discover, "discover", AsyncMock(return_value=found)):
        ok, err = await connections.test_connection("claude_code:slack")
    assert not ok and "no longer offers" in err
    row = (await db.execute_fetchall("SELECT status FROM egress_connections"))[0]
    assert row["status"] == "error"


@pytest.mark.asyncio
async def test_remove_connection_delegates(db, found):
    await _connect(db, "slack")
    with patch.object(connections, "_remove_from_keychain", side_effect=AssertionError), \
         patch.object(connections, "_remove_connection_workflows", AsyncMock(side_effect=AssertionError)):
        await connections.remove_connection("claude_code:slack")
    assert not await db.execute_fetchall("SELECT 1 FROM egress_connections")
    # Collecting stays connected.
    assert await store.get_source("slack") is not None


@pytest.mark.asyncio
async def test_connect_and_disconnect_run_the_hooks(db, found):
    with patch.object(connect, "write_identity"), patch.object(connect, "seed_rules"), \
         patch.object(connect, "remove_rules"):
        await connect.connect("slack", found=found)
        assert [r["connection_id"] for r in await db.execute_fetchall(
            "SELECT connection_id FROM egress_connections")] == ["claude_code:slack"]
        await connect.disconnect("slack")
    assert not await db.execute_fetchall("SELECT 1 FROM egress_connections")


# ── only a user click reaches the backend ────────────────────────────────


@pytest.mark.asyncio
async def test_chat_and_rules_cannot_send(db, found):
    await _connect(db, "slack")
    run = AsyncMock()
    with patch.object(cli, "run_send", run):
        out = json.loads(await tool_handlers.handle_egress_tool(
            "send_slack_message", {"channel": "C1", "message": "hi"}, "default"))
        assert out["status"] == "preview" and out["sent"] is False
        out = json.loads(await tool_handlers.handle_confirm_egress({}, "default"))
        assert out["status"] == "refused"
    run.assert_not_called()

    from laya.models.processing_rules import reject_auto_send_actions
    with pytest.raises(ValueError):
        reject_auto_send_actions([{"type": "execute_egress", "platform": "slack",
                                   "action_type": "send_message", "connection_id": "claude_code:slack"}])
