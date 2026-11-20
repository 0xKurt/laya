# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Laya's own Claude accounts, independent of the CLI's login
(claude_code/accounts.py): usage attributed to the account a turn ran on,
identity by organization, per-platform gating of collectors / My work /
actions, the `/usage` poll, the sign-in state endpoint and the parsing of the
authorize URL the CLI prints."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import accounts, cli, discover, my_work, service, store, work_actions
from laya.pipeline import agent_budget

T = {"five_hour": 0.9, "seven_day": 0.8}
FUTURE = int(time.time()) + 3 * 86400

SLACK_A = "mcp__claude_ai_Slack__"
LINEAR_A = "mcp__claude_ai_Linear__"
LINEAR_B = "mcp__linear-server__"


def _init(*prefixes: str) -> dict:
    """A system/init message with the Slack and/or Linear tools of the given server prefixes."""
    tools = []
    for p in prefixes:
        if "Slack" in p:
            tools += [f"{p}slack_search_public_and_private", f"{p}slack_read_thread",
                      f"{p}slack_read_channel", f"{p}slack_send_message"]
        else:
            tools += [f"{p}get_notifications", f"{p}list_issues", f"{p}get_issue",
                      f"{p}save_issue", f"{p}save_comment", f"{p}list_issue_statuses"]
    return {"tools": tools, "mcp_servers": []}


def _matrix(**states: str) -> str:
    return json.dumps({"connectors": {k: {"state": v} for k, v in states.items()},
                       "checked_at": "2026-11-20T08:00:00+00:00", "error": None})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(accounts, "_route", accounts._Route())
    monkeypatch.setattr(accounts, "_flow", None)
    monkeypatch.setattr(accounts, "_last_poll", {})
    monkeypatch.setattr(accounts, "_default_status", (time.monotonic(), {
        "logged_in": True, "email": "cli@example.com", "plan": "max", "org_name": None, "org_id": "org-cli"}))
    monkeypatch.setattr(accounts, "_thresholds", lambda: dict(T))
    discover.reset_account_cache()
    discover.reset_cache()

    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude")

    with patch.object(cli, "read_init", side_effect=_refuse), \
         patch.object(cli, "cli_version", side_effect=_refuse):
        yield
    discover.reset_account_cache()
    discover.reset_cache()


_seq = [0]


async def _add(account_id: str, email: str | None = None, org_id: str | None = None, logged_in: int = 1,
               init: dict | None = None, matrix: dict | None = None):
    path = accounts.ACCOUNTS_DIR / account_id
    path.mkdir(parents=True, exist_ok=True)
    _seq[0] += 1
    db = await accounts._db()
    await db.execute(
        "INSERT INTO claude_code_accounts (id, config_dir, email, plan, logged_in, created_at, org_id) "
        "VALUES (?, ?, ?, 'max', ?, ?, ?)",
        (account_id, str(path), email or f"{account_id}@example.com", logged_in,
         (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=_seq[0])).isoformat(),
         org_id or f"org-{account_id}"),
    )
    await db.commit()
    if init is not None:
        discover.store_account_listing(account_id, init)
    if matrix is not None:
        await store.set_state("accounts", f"connectors:{account_id}", _matrix(**matrix))
    return path


def _info(five=None, seven=None, status="allowed", resets=FUTURE):
    windows = {}
    if five is not None:
        windows["five_hour"] = {"utilization": five, "resetsAt": resets, "status": status}
    if seven is not None:
        windows["seven_day"] = {"utilization": seven, "resetsAt": resets, "status": status}
    return {"status": status, "resetsAt": resets, "unifiedWindows": windows}


# ── 1. usage recorded under the account the turn ran on ──────────────────


@pytest.mark.asyncio
async def test_collector_rejection_is_recorded_for_the_account_that_ran_it(db):
    path = await _add("acct1")
    out = [json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": FUTURE}}),
           json.dumps({"type": "result", "is_error": True, "result": "You've hit your limit", "usage": {}})]
    seen = {}

    async def fake_process(args, timeout, env=None):
        seen["env"] = env
        return out

    with patch.object(cli, "_run_process", side_effect=fake_process), \
         patch.object(cli, "get_agent_binary", return_value="claude"), \
         patch("laya.llm.client.log_to_audit", AsyncMock()):
        with pytest.raises(cli.ClaudeCodeError):
            await cli.run_collect("slack", "p", {"type": "object"}, ["t"],
                                  env={"CLAUDE_CONFIG_DIR": str(path)}, account_id="acct1")
    assert seen["env"] == {"CLAUDE_CONFIG_DIR": str(path)}
    assert (await agent_budget.get_rate_limit_raw("claude_code@acct1"))["status"] == "rejected"
    # The CLI login's row stays untouched.
    assert await agent_budget.get_rate_limit_raw("claude_code@org:org-cli") is None


# ── 2. identity ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cli_login_switch_starts_from_a_clean_row(db, monkeypatch):
    await accounts.record("default", {"status": "rejected", "resetsAt": FUTURE})
    assert (await accounts.windows_of("default"))
    # /login to another account in a Claude Code session.
    monkeypatch.setattr(accounts, "_default_status", (time.monotonic(), {
        "logged_in": True, "email": "other@example.com", "org_id": "org-other"}))
    assert await accounts.windows_of("default") == {}


@pytest.mark.asyncio
async def test_legacy_slot_rows_are_dropped(db):
    for row in ("claude_code", "claude_code@default"):
        await agent_budget.record_rate_limit(row, {"status": "rejected", "resetsAt": FUTURE})
    db_ = await accounts._db()
    db_._laya_claude_accounts_table = False
    await accounts._db()
    assert await agent_budget.get_rate_limit_raw("claude_code") is None
    assert await agent_budget.get_rate_limit_raw("claude_code@default") is None


@pytest.mark.asyncio
async def test_identity_change_clears_the_accounts_usage(db):
    await _add("acct1", org_id="org-a")
    await accounts.record("acct1", _info(seven=0.95, status="allowed_warning"))
    await store.set_state("accounts", "usage:acct1", json.dumps({"windows": {}, "at": time.time()}))
    row = await accounts.get_row("acct1")
    await accounts._store_status(row, {"logged_in": True, "email": "b@example.com", "plan": "max",
                                       "org_name": None, "org_id": "org-b"})
    assert await agent_budget.get_rate_limit_raw("claude_code@acct1") is None
    assert await accounts.stored_usage("acct1") is None
    assert (await accounts.get_row("acct1"))["org_id"] == "org-b"


def test_auth_status_reads_the_org_id():
    out = json.dumps({"loggedIn": True, "email": "a@b.c", "orgId": "o-1", "subscriptionType": "max"})
    assert accounts.parse_auth_status(out)["org_id"] == "o-1"


# ── 3. per-platform gating ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_for_platform_uses_only_accounts_with_the_connector(db):
    await _add("acct1", init=_init(SLACK_A), matrix={"slack": "connected", "linear": "missing"})
    path2 = await _add("acct2", init=_init(LINEAR_B), matrix={"linear": "connected"})
    got = await accounts.pick_for_platform("linear")
    assert got.account_id == "acct2" and got.env == {"CLAUDE_CONFIG_DIR": str(path2)}
    # The account's own tool prefix, and a deny list from its own listing.
    assert got.tools["list_issues"] == f"{LINEAR_B}list_issues"
    assert f"{LINEAR_B}save_issue" in got.all_tools and not any(SLACK_A in t for t in got.all_tools)
    assert (await accounts.pick_for_platform("slack")).account_id == "acct1"
    assert await accounts.pick_for_platform("notion") is None


@pytest.mark.asyncio
async def test_a_connector_check_without_the_tools_is_not_enough(db):
    # The matrix says connected, but the listing lacks the required roles.
    await _add("acct1", init={"tools": [f"{LINEAR_A}list_issues"], "mcp_servers": []},
               matrix={"linear": "connected"})
    assert await accounts.pick_for_platform("linear") is None


@pytest.mark.asyncio
async def test_tool_prefixes_differ_per_account(db):
    await _add("acct1", init=_init(LINEAR_A), matrix={"linear": "connected"})
    await _add("acct2", init=_init(LINEAR_B), matrix={"linear": "connected"})
    await accounts.record("acct1", _info(seven=0.95, status="allowed_warning"))
    got = await accounts.pick_for_platform("linear")
    assert got.account_id == "acct2" and got.tools["get_issue"] == f"{LINEAR_B}get_issue"


@pytest.mark.asyncio
async def test_sweep_without_an_eligible_account_keeps_the_cursor(db):
    await _add("acct1", init=_init(SLACK_A), matrix={"slack": "connected"})
    cursor = "2026-11-20T06:00:00+00:00"
    await store.upsert_source("linear", config=store.default_config("linear"), cursor=cursor,
                              tools={"list_issues": f"{LINEAR_A}list_issues"})
    collector = AsyncMock(return_value=[])
    with patch.dict(service.COLLECTORS, {"linear": collector}), \
         patch.object(service, "blocked_reason", AsyncMock(return_value=None)):
        out = await service.sweep("linear")
    assert out == {"events": 0, "error": service.NO_ACCOUNT_ERROR}
    collector.assert_not_awaited()
    source = await store.get_source("linear")
    assert source["cursor"] == cursor and source["last_error"] == service.NO_ACCOUNT_ERROR


@pytest.mark.asyncio
async def test_sweep_runs_every_turn_on_the_picked_account(db):
    path = await _add("acct2", init=_init(LINEAR_B, SLACK_A), matrix={"linear": "connected"})
    await store.upsert_source("linear", config=store.default_config("linear"),
                              tools={"list_issues": f"{LINEAR_A}list_issues"})
    seen = {}

    async def collector(source, since):
        seen.update(source)
        return []

    with patch.dict(service.COLLECTORS, {"linear": collector}), \
         patch.object(service, "blocked_reason", AsyncMock(return_value=None)), \
         patch("laya.claude_code.recheck.recheck", AsyncMock(return_value=[])):
        out = await service.sweep("linear")
    assert out == {"events": 0, "error": None}
    assert seen["account_id"] == "acct2" and seen["account_env"] == {"CLAUDE_CONFIG_DIR": str(path)}
    assert seen["tools"]["list_issues"] == f"{LINEAR_B}list_issues"
    assert f"{SLACK_A}slack_send_message" in seen["deny_tools"]
    assert (await store.get_source("linear"))["cursor"]


@pytest.mark.asyncio
async def test_common_run_turn_passes_the_account_on(db):
    from laya.claude_code.platforms import common

    collect = AsyncMock(return_value={})
    source = {"tools": {"search": "t"}, "config": {}, "deny_tools": ["x"],
              "account_env": {"CLAUDE_CONFIG_DIR": "/d"}, "account_id": "acct9"}
    with patch.object(cli, "run_collect", collect):
        await common.run_turn(source, "slack", "p", {})
    assert collect.await_args.kwargs["env"] == {"CLAUDE_CONFIG_DIR": "/d"}
    assert collect.await_args.kwargs["account_id"] == "acct9"


@pytest.mark.asyncio
async def test_linked_documents_use_the_accounts_tool_names():
    from laya.claude_code import links

    source = {"account_platform_tools": {"notion": {"fetch": "mcp__notion__notion-fetch"}}}
    assert links.doc_tools(source) == {"notion": "mcp__notion__notion-fetch"}


@pytest.mark.asyncio
async def test_my_work_linear_runs_on_an_account_with_linear(db):
    path = await _add("acct2", init=_init(LINEAR_B), matrix={"linear": "connected"})
    collect = AsyncMock(return_value={"assigned": [], "created": []})
    with patch.object(cli, "run_collect", collect):
        await my_work.fetch_linear({"config": {}})
    args, kwargs = collect.await_args
    assert args[3] == [f"{LINEAR_B}list_issues"]
    assert kwargs["env"] == {"CLAUDE_CONFIG_DIR": str(path)} and kwargs["account_id"] == "acct2"


@pytest.mark.asyncio
async def test_linear_actions_run_on_the_picked_account_and_never_fail_over(db):
    path = await _add("acct2", init=_init(LINEAR_B), matrix={"linear": "connected"})
    await store.upsert_source("linear", config=store.default_config("linear"))
    send = AsyncMock(side_effect=cli.ClaudeCodeError("You've hit your limit"))
    with patch.object(cli, "run_send", send):
        with pytest.raises(work_actions.ActionError):
            await work_actions.linear_action("TRAD-1", "comment", "hello")
    assert send.await_count == 1
    assert send.await_args.kwargs["env"] == {"CLAUDE_CONFIG_DIR": str(path)}
    assert send.await_args.args[2] == f"{LINEAR_B}save_comment"


@pytest.mark.asyncio
async def test_linear_actions_without_an_account_with_linear_are_refused(db):
    await _add("acct1", init=_init(SLACK_A), matrix={"slack": "connected"})
    await store.upsert_source("linear", config=store.default_config("linear"))
    with pytest.raises(work_actions.ActionError) as e:
        await work_actions.linear_action("TRAD-1", "comment", "hello")
    assert e.value.status == 409


# ── 4. usage poll ────────────────────────────────────────────────────────

USAGE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 37% used · resets 5pm (Europe/Berlin)\n"
    "Current week (all models): 100% used · resets Nov 25 at 4am (Europe/Berlin)\n"
    "Current week (Fable): 12% used · resets Nov 25 at 4am (Europe/Berlin)\n\n"
    "What's contributing to your limits usage?\n  95%  Run fewer sessions at once\n"
)


def test_parse_usage():
    out = accounts.parse_usage(USAGE_TEXT)
    assert out["windows"]["five_hour"] == {"utilization": 0.37, "resets": "5pm (Europe/Berlin)"}
    assert out["windows"]["seven_day"]["utilization"] == 1.0
    assert out["models"] == [{"label": "Fable", "percent": 12, "resets": "Nov 25 at 4am (Europe/Berlin)"}]
    # Contributor lines ("95%  Run fewer…") are not usage.
    assert accounts.parse_usage("Last 24h\n  95%  Run fewer sessions at once") is None
    # A window without its line stays unknown.
    only_week = accounts.parse_usage("Current week (all models): 2% used · resets Nov 26 at 4am")
    assert "five_hour" not in only_week["windows"]


def _usage_json(text: str) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": text, "local_command": "usage"})


@pytest.mark.asyncio
async def test_poll_stores_usage_and_routing_uses_it(db, monkeypatch):
    await _add("acct1")
    await _add("acct2")
    monkeypatch.setattr(accounts, "auth_status", AsyncMock(return_value={
        "logged_in": True, "email": "acct1@example.com", "plan": "max", "org_name": None, "org_id": "org-acct1"}))
    run_cli = AsyncMock(return_value=(0, _usage_json(USAGE_TEXT), ""))
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    monkeypatch.setattr(accounts, "get_agent_binary", lambda _a: "claude")
    assert (await accounts.poll_usage("acct1"))["polled"]
    args = run_cli.await_args.args[0]
    assert args[1:4] == ["-p", "/usage", "--output-format"]
    assert run_cli.await_args.args[1] == {"CLAUDE_CONFIG_DIR": str(accounts.ACCOUNTS_DIR / "acct1")}
    w = await accounts.windows_of("acct1")
    assert w["seven_day"]["utilization"] == 1.0 and w["seven_day"]["resets_text"].startswith("Nov 25")
    # acct1 is at 100 % of its week: routing uses acct2.
    assert (await accounts.pick())[0] == "acct2"


@pytest.mark.asyncio
async def test_poll_of_a_signed_out_account_marks_it_and_reads_no_usage(db, monkeypatch):
    await _add("acct1")
    monkeypatch.setattr(accounts, "auth_status", AsyncMock(return_value={"logged_in": False}))
    run_cli = AsyncMock()
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    out = await accounts.poll_usage("acct1")
    assert out == {"polled": False, "logged_in": False, "error": None}
    run_cli.assert_not_awaited()
    assert (await accounts.get_row("acct1"))["logged_in"] == 0
    assert await accounts.eligible_ids() == []


@pytest.mark.asyncio
async def test_poll_without_a_usage_block_keeps_the_last_known_values(db, monkeypatch):
    await _add("acct1")
    monkeypatch.setattr(accounts, "auth_status", AsyncMock(return_value={
        "logged_in": True, "email": "acct1@example.com", "org_id": "org-acct1", "plan": "max", "org_name": None}))
    monkeypatch.setattr(accounts, "get_agent_binary", lambda _a: "claude")
    monkeypatch.setattr(accounts, "_run_cli", AsyncMock(return_value=(0, _usage_json(USAGE_TEXT), "")))
    await accounts.poll_usage("acct1", force=True)
    monkeypatch.setattr(accounts, "_run_cli", AsyncMock(return_value=(1, _usage_json("Unknown command"), "")))
    out = await accounts.poll_usage("acct1", force=True)
    assert not out["polled"] and out["error"]
    assert (await accounts.windows_of("acct1"))["five_hour"]["utilization"] == 0.37
    # A later poll without the session line keeps the session's last value.
    monkeypatch.setattr(accounts, "_run_cli", AsyncMock(return_value=(
        0, _usage_json("Current week (all models): 50% used · resets Nov 25 at 4am"), "")))
    await accounts.poll_usage("acct1", force=True)
    w = await accounts.windows_of("acct1")
    assert w["five_hour"]["utilization"] == 0.37 and w["seven_day"]["utilization"] == 0.5


@pytest.mark.asyncio
async def test_poll_is_skipped_during_a_sign_in_and_after_fresh_turn_data(db, monkeypatch):
    await _add("acct1")
    monkeypatch.setattr(accounts, "auth_status", AsyncMock(return_value={
        "logged_in": True, "email": "acct1@example.com", "org_id": "org-acct1", "plan": "max", "org_name": None}))
    run_cli = AsyncMock(return_value=(0, _usage_json(USAGE_TEXT), ""))
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    monkeypatch.setattr(accounts, "_flow", accounts.LoginFlow(
        id="f", kind="connector", account_id="acct1", new_account=False))
    assert (await accounts.poll_usage("acct1", force=True))["error"] == "sign-in running"
    monkeypatch.setattr(accounts, "_flow", None)
    await accounts.record("acct1", _info(five=0.1, seven=0.2))
    assert not (await accounts.poll_usage("acct1"))["polled"]
    run_cli.assert_not_awaited()
    assert (await accounts.poll_usage("acct1", force=True))["polled"]


def test_merge_keeps_rejection_and_epoch_resets_from_the_rate_signal():
    now = time.time()
    windows = {"seven_day": {"utilization": 0.5, "resets_at": FUTURE, "status": "rejected"}}
    polled = {"windows": {"seven_day": {"utilization": 0.4, "resets": "Tue"}}, "at": now}
    out = accounts.merge_usage(windows, polled, rate_updated=now - 60, now=now)
    assert out["seven_day"] == {"utilization": 0.4, "resets_at": FUTURE, "status": "rejected", "resets_text": "Tue"}
    # A poll older than the window's reset no longer counts.
    reset = now - 30
    windows = {"five_hour": {"utilization": 0.0, "resets_at": reset, "status": "allowed"}}
    polled = {"windows": {"five_hour": {"utilization": 0.9, "resets": None}}, "at": now - 600}
    assert accounts.merge_usage(windows, polled, now - 900, now)["five_hour"]["utilization"] == 0.0
    # A turn newer than the poll wins.
    windows = {"five_hour": {"utilization": 0.2, "resets_at": FUTURE, "status": "allowed"}}
    polled = {"windows": {"five_hour": {"utilization": 0.9}}, "at": now - 600}
    assert accounts.merge_usage(windows, polled, now - 10, now)["five_hour"]["utilization"] == 0.2


@pytest.mark.asyncio
async def test_due_polls_are_spaced(db, monkeypatch):
    poll = AsyncMock(return_value={"polled": True})
    monkeypatch.setattr(accounts, "poll_usage", poll)
    # Nothing to poll while Laya does not use Claude Code at all.
    assert await accounts.poll_due_usage() == [] and poll.await_count == 0
    await _add("acct1")
    assert await accounts.poll_due_usage() == ["default", "acct1"]
    accounts._last_poll.update({"default": time.monotonic(), "acct1": time.monotonic()})
    assert await accounts.poll_due_usage() == []


# ── 5. sign-in state endpoint ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_endpoint_returns_the_engine_state(db, monkeypatch):
    from laya.main import app

    flow = accounts.LoginFlow(id="f1", kind="connector", account_id="acct1", new_account=False,
                              server="linear-server", platform="linear", state="waiting_code",
                              url="https://mcp.linear.app/authorize?x=1")
    monkeypatch.setattr(accounts, "_flow", flow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = (await client.get("/claude-code/accounts/login")).json()["login"]
    assert login["id"] == "f1" and login["state"] == "waiting_code" and login["url"].startswith("https://")
    started = datetime.fromisoformat(login["started_at"])
    assert datetime.fromisoformat(login["expires_at"]) - started == timedelta(seconds=accounts.LOGIN_TIMEOUT)


# ── 6. authorize URL parsing ─────────────────────────────────────────────

LINEAR_URL = ("https://mcp.linear.app/authorize?response_type=code&client_id=https%3A%2F%2Fclaude.ai"
              "&redirect_uri=http%3A%2F%2Flocalhost%3A65332%2Fcallback&state=N-i3B&scope=read+write")


def test_parse_osc8_with_bel_as_printed_by_mcp_login():
    # Recorded from `claude mcp login linear-server --no-browser` (2.1.311).
    raw = (f'Starting authentication for "linear-server"…\r\nVisit this URL to authorize:\r\n  '
           f"\x1b]8;;{LINEAR_URL}\x07\x1b[94m{LINEAR_URL}\x1b[39m\x1b]8;;\x07\r\n\r\n"
           "Waiting for authorization… (^C to cancel)\r\n\x1b[1G\x1b[0JOr paste the redirect URL here: \x1b[33G")
    assert accounts.parse_login_url(raw) == LINEAR_URL


def test_parse_osc8_with_st_and_params():
    raw = f"Visit:\n\x1b]8;id=42;{LINEAR_URL}\x1b\\\x1b[1m\x1b[94mopen\x1b[0m\x1b]8;;\x1b\\\n"
    assert accounts.parse_login_url(raw) == LINEAR_URL


def test_parse_csi_coloured_url_and_colour_codes_inside_it():
    assert accounts.parse_login_url(f"Open \x1b[38;5;12m{LINEAR_URL}\x1b[0m now") == LINEAR_URL
    split = LINEAR_URL[:30] + "\x1b[39m\x1b[94m" + LINEAR_URL[30:]
    assert accounts.parse_login_url(f"\x1b[?25l{split}\x1b[>4;2m") == LINEAR_URL


def test_parse_first_https_url_wins():
    raw = "see https://first.example/a then https://second.example/b"
    assert accounts.parse_login_url(raw) == "https://first.example/a"
    assert accounts.parse_login_url("no link here http://localhost:1/x") is None
