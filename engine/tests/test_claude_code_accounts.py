# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for Claude accounts (claude_code/accounts.py): usage windows, routing,
failover in the agent backend, the multi-account budget pause, the PTY sign-in
(against a fake `claude` script), removal, the connector matrix and the API."""

from __future__ import annotations

import asyncio
import json
import stat
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import accounts, cli
from laya.llm import agent_backend as ab
from laya.pipeline import agent_budget

T = {"five_hour": 0.9, "seven_day": 0.8}
FUTURE = int(time.time()) + 3 * 86400
PAST = int(time.time()) - 60


def _info(five=None, seven=None, status="allowed", resets=FUTURE):
    windows = {}
    if five is not None:
        windows["five_hour"] = {"utilization": five, "resetsAt": resets, "status": status}
    if seven is not None:
        windows["seven_day"] = {"utilization": seven, "resetsAt": resets, "status": status}
    return {"status": status, "resetsAt": resets, "unifiedWindows": windows}


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    monkeypatch.setattr(accounts, "_route", accounts._Route())
    monkeypatch.setattr(accounts, "_flow", None)
    monkeypatch.setattr(accounts, "_default_status", (time.monotonic(), {
        "logged_in": True, "email": "kurt@example.com", "plan": "max", "org_name": None,
        "org_id": "org-cli"}))
    monkeypatch.setattr(accounts, "_thresholds", lambda: dict(T))
    monkeypatch.setattr(accounts, "_last_poll", {})
    from laya.claude_code import discover

    discover.reset_account_cache()
    yield
    flow = accounts._flow
    if flow is not None and flow.proc is not None and hasattr(flow.proc, "proc"):
        if flow.proc.proc.poll() is None:
            flow.proc.proc.kill()
            flow.proc.proc.wait()


_created = [0]


async def _add_row(account_id="acct2", email="second@example.com", logged_in=1, org_id=None):
    path = accounts.ACCOUNTS_DIR / account_id
    path.mkdir(parents=True, exist_ok=True)
    db = await accounts._db()
    _created[0] += 1
    created = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=_created[0])).isoformat()
    await db.execute(
        "INSERT INTO claude_code_accounts (id, config_dir, email, plan, logged_in, created_at, org_id) "
        "VALUES (?, ?, ?, 'max', ?, ?, ?)",
        (account_id, str(path), email, logged_in, created, org_id or f"org-{account_id}"),
    )
    await db.commit()
    return path


# ── usage windows ────────────────────────────────────────────────────────


def test_windows_come_from_unified_windows():
    raw = {"status": "allowed_warning", "resetsAt": FUTURE, "rateLimitType": "seven_day",
           "utilization": 0.88, "unifiedWindows": {
               "seven_day": {"utilization": 0.88, "resetsAt": FUTURE, "status": "allowed_warning"},
               "five_hour": {"utilization": 0.2, "resetsAt": FUTURE, "status": "allowed"}}}
    w = agent_budget.rate_limit_windows(raw)
    assert w["seven_day"] == {"utilization": 0.88, "resets_at": FUTURE, "status": "allowed_warning"}
    assert w["five_hour"]["utilization"] == 0.2


def test_top_level_window_fills_in_and_status_without_type_is_kept():
    w = agent_budget.rate_limit_windows(
        {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": FUTURE})
    assert w == {"five_hour": {"utilization": 0.5, "resets_at": FUTURE, "status": "allowed"}}
    w = agent_budget.rate_limit_windows({"status": "rejected", "resetsAt": FUTURE})
    assert w["unknown"]["status"] == "rejected"
    assert agent_budget.rate_limit_windows({}) == {}


def test_window_resets_after_resets_at():
    w = agent_budget.rate_limit_windows(_info(seven=0.95, status="allowed_warning", resets=PAST))
    assert w["seven_day"]["utilization"] == 0.0 and w["seven_day"]["status"] == "allowed"
    w = agent_budget.rate_limit_windows({"status": "rejected", "resetsAt": PAST})
    assert w["unknown"]["status"] == "allowed"


def test_rejection_overrides_the_window_list():
    raw = {**_info(five=0.5), "status": "rejected", "rateLimitType": "five_hour"}
    w = agent_budget.rate_limit_windows(raw)
    assert w["five_hour"]["status"] == "rejected"


def test_over_and_headroom():
    assert accounts.account_over({"seven_day": {"utilization": 0.8, "status": "allowed"}}, T)
    assert not accounts.account_over({"seven_day": {"utilization": 0.79, "status": "allowed"}}, T)
    assert accounts.account_over({"five_hour": {"utilization": 0.1, "status": "rejected"}}, T)
    assert accounts.account_over({"seven_day": {"utilization": None, "status": "allowed_warning"}}, T)
    # The hysteresis margin.
    assert accounts.account_over({"seven_day": {"utilization": 0.75, "status": "allowed"}}, T, 0.10)
    assert accounts.headroom({}, T) == 1.0
    assert accounts.headroom({"seven_day": {"utilization": 0.5}}, T) == pytest.approx(0.3)


# ── routing decisions ────────────────────────────────────────────────────

ORDER = ["default", "b", "c"]
LOW = {"seven_day": {"utilization": 0.3, "status": "allowed"}}
MID = {"seven_day": {"utilization": 0.75, "status": "allowed"}}
HIGH = {"seven_day": {"utilization": 0.85, "status": "allowed_warning"}}
REJ = {"five_hour": {"utilization": 1.0, "status": "rejected"}}
NOW = 10_000.0
LONG_AGO = NOW - accounts.DWELL_SECONDS - 1
JUST_NOW = NOW - 60


def test_first_pick_is_the_first_account_with_room():
    assert accounts.choose(ORDER, {"default": LOW}, T, None, 0, NOW) == "default"
    assert accounts.choose(ORDER, {"default": HIGH, "b": LOW}, T, None, 0, NOW) == "b"


def test_sticky_while_under_threshold():
    w = {"default": LOW, "b": LOW, "c": LOW}
    assert accounts.choose(ORDER, w, T, "c", JUST_NOW, NOW) == "c"


def test_switch_at_threshold_only_after_dwell():
    w = {"default": HIGH, "b": MID, "c": LOW}
    assert accounts.choose(ORDER, w, T, "default", JUST_NOW, NOW) == "default"
    # After the dwell: the account with most headroom.
    assert accounts.choose(ORDER, w, T, "default", LONG_AGO, NOW) == "c"


def test_rejection_switches_at_once():
    assert accounts.choose(ORDER, {"default": REJ, "b": LOW, "c": HIGH}, T, "default", JUST_NOW, NOW) == "b"


def test_return_to_preferred_only_below_threshold_minus_hysteresis():
    # Default at 75 %: below its 80 % threshold but not 10 points below → stay on b.
    assert accounts.choose(ORDER, {"default": MID, "b": LOW}, T, "b", LONG_AGO, NOW) == "b"
    # Default at 30 %: back to the default, after the dwell only.
    assert accounts.choose(ORDER, {"default": LOW, "b": LOW}, T, "b", LONG_AGO, NOW) == "default"
    assert accounts.choose(ORDER, {"default": LOW, "b": LOW}, T, "b", JUST_NOW, NOW) == "b"


def test_every_account_over_keeps_the_current_one():
    w = {"default": HIGH, "b": HIGH, "c": REJ}
    assert accounts.choose(ORDER, w, T, "b", LONG_AGO, NOW) == "b"


# ── routing against the database ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_without_added_accounts_is_the_default(db):
    assert await accounts.pick() == ("default", {})


@pytest.mark.asyncio
async def test_pick_moves_to_an_added_account_and_back(db, monkeypatch):
    await _add_row("acct1", email="first@example.com")
    path = await _add_row()
    await accounts.record("acct1", _info(seven=0.9, status="allowed_warning"))
    assert await accounts.pick() == ("acct2", {"CLAUDE_CONFIG_DIR": str(path)})
    # Sticky: the first account recovering a little does not move routing back.
    await accounts.record("acct1", _info(seven=0.75))
    assert (await accounts.pick())[0] == "acct2"
    # Well below its threshold, after the dwell, routing returns to the first account.
    await accounts.record("acct1", _info(seven=0.2))
    accounts._route.since -= accounts.DWELL_SECONDS + 1
    assert (await accounts.pick())[0] == "acct1"


@pytest.mark.asyncio
async def test_added_accounts_replace_the_cli_login(db):
    """Once an account is added, Laya never runs on whatever the CLI is logged in to."""
    path = await _add_row("acct1", email="kurt@example.com")  # the same person as the CLI login is fine
    assert await accounts.eligible_ids() == ["acct1"]
    assert await accounts.pick() == ("acct1", {"CLAUDE_CONFIG_DIR": str(path)})


@pytest.mark.asyncio
async def test_a_first_sign_in_in_progress_keeps_the_cli_login(db):
    db_ = await accounts._db()
    await db_.execute(
        "INSERT INTO claude_code_accounts (id, config_dir, created_at) VALUES ('new', ?, '2026-01-01')",
        (str(accounts.ACCOUNTS_DIR / "new"),),
    )
    await db_.commit()
    assert await accounts.eligible_ids() == ["default"]


@pytest.mark.asyncio
async def test_no_usable_added_account_never_falls_back_to_the_cli(db, monkeypatch):
    await _add_row("out", logged_in=0)
    assert await accounts.eligible_ids() == []
    with pytest.raises(accounts.NoEligibleAccount):
        await accounts.pick()
    blocked, _, reason = await accounts.native_block(T)
    assert blocked and reason.startswith(accounts.ACCOUNTS_PAUSE_REASON)


@pytest.mark.asyncio
async def test_pick_failure_falls_back_to_the_first_added_account(db, monkeypatch):
    path = await _add_row("acct1")
    await _add_row()
    monkeypatch.setattr(accounts, "windows_of", AsyncMock(side_effect=RuntimeError("db gone")))
    assert await accounts.pick() == ("acct1", {"CLAUDE_CONFIG_DIR": str(path)})


@pytest.mark.asyncio
async def test_signed_out_or_duplicate_accounts_are_not_eligible(db):
    await _add_row("out", logged_in=0)
    await _add_row("ok", email="other@example.com", org_id="org-x")
    await _add_row("dup", email="third@example.com", org_id="org-x")
    await _add_row("ok2", email="fourth@example.com")
    assert await accounts.eligible_ids() == ["ok", "ok2"]


@pytest.mark.asyncio
async def test_record_stores_per_account_rows_and_rejections(db):
    await accounts.record("acct2", _info(five=0.4))
    await accounts.record(None, _info(seven=0.5))
    assert (await agent_budget.get_rate_limit_raw("claude_code@acct2"))["unifiedWindows"]["five_hour"]
    # The CLI login is keyed by the organization it is logged in to.
    assert (await agent_budget.get_rate_limit_raw("claude_code@org:org-cli"))["unifiedWindows"]["seven_day"]
    assert await agent_budget.get_rate_limit_raw("claude_code") is None
    # A rejection without any signal holds the account for a while.
    await accounts.record("acct2", None, rejected=True)
    w = await accounts.windows_of("acct2")
    assert any(x["status"] == "rejected" for x in w.values())
    # Nothing to record.
    await accounts.record("acct3", None)
    assert await agent_budget.get_rate_limit_raw("claude_code@acct3") is None


def test_is_rejection():
    assert accounts.is_rejection({"status": "rejected"})
    assert accounts.is_rejection(None, "Claude AI usage limit reached|1795532400")
    assert accounts.is_rejection(None, "You've hit your limit · resets 5pm")
    assert not accounts.is_rejection({"status": "allowed_warning"}, "MCP server failed")


# ── agent backend failover ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_routed_retries_once_on_the_next_account(db, monkeypatch):
    path1 = await _add_row("acct1", email="first@example.com")
    path = await _add_row()
    calls = []

    async def fake_run(agent_id, *args, account_id=None, account_env=None):
        calls.append((account_id, account_env))
        if account_id == "acct1":
            await accounts.record("acct1", None, rejected=True)
            raise ab.AgentRateLimited("hit your limit")
        return {"content": "ok"}

    monkeypatch.setattr(ab, "_run_agent", fake_run)
    result = await ab._run_agent_routed("claude_code", "bin", "s", "u", None, None, True, 30.0)
    assert result["account_id"] == "acct2"
    assert calls == [("acct1", {"CLAUDE_CONFIG_DIR": str(path1)}), ("acct2", {"CLAUDE_CONFIG_DIR": str(path)})]
    # Routing stays on the account that worked.
    assert accounts._route.current == "acct2"


@pytest.mark.asyncio
async def test_run_agent_routed_without_another_account_raises(db, monkeypatch):
    async def fake_run(agent_id, *args, account_id=None, account_env=None):
        raise ab.AgentRateLimited("hit your limit")

    monkeypatch.setattr(ab, "_run_agent", fake_run)
    with pytest.raises(ab.AgentRateLimited):
        await ab._run_agent_routed("claude_code", "bin", "s", "u", None, None, True, 30.0)


@pytest.mark.asyncio
async def test_run_agent_records_the_rejection_before_raising(db, monkeypatch):
    lines = [
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {**_info(five=1.0), "status": "rejected",
                                                                    "rateLimitType": "five_hour"}}),
        json.dumps({"type": "result", "is_error": True, "result": "You've hit your limit"}),
    ]

    class Proc:
        stderr_output = ""

        async def spawn(self, args, cwd=None, env=None):
            self.env = env

        async def read_lines(self, idle_timeout=0):
            for line in lines:
                yield line

        async def wait(self):
            return 1

        async def terminate(self):
            pass

    monkeypatch.setattr(ab, "AgentProcess", Proc)
    with pytest.raises(ab.AgentRateLimited):
        await ab._run_agent("claude_code", "claude", "s", "u", None, None, True, 30.0,
                            account_id="acct2", account_env={"CLAUDE_CONFIG_DIR": "/x"})
    w = await accounts.windows_of("acct2")
    assert w["five_hour"]["status"] == "rejected"


class StreamProc:
    """Fake AgentProcess for streaming turns; each instance replays the next script."""

    scripts: list[list[str]] = []
    envs: list = []

    def __init__(self):
        self.stderr_output = ""
        self.lines = StreamProc.scripts.pop(0)

    async def spawn(self, args, cwd=None, env=None):
        StreamProc.envs.append(env)

    async def read_lines(self, idle_timeout=300.0):
        for line in self.lines:
            yield line

    async def wait(self):
        return 0

    async def terminate(self):
        pass


def _chunk(text):
    return [
        json.dumps({"type": "stream_event", "parent_tool_use_id": None,
                    "event": {"type": "message_start", "message": {"id": "m1"}}}),
        json.dumps({"type": "stream_event", "parent_tool_use_id": None, "event": {
            "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}}),
    ]


def _res(text, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": text, "usage": {}})


@pytest.fixture
def stream_env(monkeypatch):
    StreamProc.scripts, StreamProc.envs = [], []
    monkeypatch.setattr(ab, "AgentProcess", StreamProc)
    monkeypatch.setattr(ab, "get_agent_binary", lambda agent_id: "claude")
    monkeypatch.setattr("laya.pipeline.agent_budget.enforce_daily_call_limit", AsyncMock())
    monkeypatch.setattr("laya.pipeline.queue.get_model_timeout", lambda: 30.0)
    audit = AsyncMock()
    monkeypatch.setattr("laya.llm.client.log_to_audit", audit)
    monkeypatch.setattr("laya.tasks.create_task", lambda coro, **kw: coro.close())
    return audit


async def _stream():
    msgs = [{"role": "user", "content": "hi"}]
    return [ev async for ev in ab.agent_llm_stream("agent/claude_code/haiku", msgs)]


@pytest.mark.asyncio
async def test_stream_fails_over_before_the_first_chunk(db, stream_env):
    path1 = await _add_row("acct1", email="first@example.com")
    path = await _add_row()
    StreamProc.scripts = [[_res("You've hit your limit", is_error=True)], [*_chunk("hello"), _res("hello")]]
    events = await _stream()
    assert [e.type for e in events] == ["chunk", "done"]
    assert StreamProc.envs == [{"CLAUDE_CONFIG_DIR": str(path1)}, {"CLAUDE_CONFIG_DIR": str(path)}]
    assert stream_env.await_args.kwargs["metadata"]["claude_account"] == "acct2"


@pytest.mark.asyncio
async def test_stream_does_not_fail_over_after_a_chunk(db, stream_env):
    await _add_row()
    StreamProc.scripts = [[*_chunk("partial"), _res("You've hit your limit", is_error=True)]]
    events = await _stream()
    assert [e.type for e in events] == ["chunk", "error"]
    assert len(StreamProc.envs) == 1


# ── collector turns record usage (stream-json) ───────────────────────────


def test_cli_turns_use_stream_json():
    args = cli.build_args("claude", "p", tools=["t"], model="haiku", schema=None)
    assert args[args.index("--output-format") + 1] == "stream-json" and "--verbose" in args


def test_parse_turn_output_reads_result_and_rate_limit():
    info = _info(five=0.3)
    data, rl = cli.parse_turn_output([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": info}),
        json.dumps({"type": "result", "is_error": False, "structured_output": {"a": 1}}),
    ])
    assert data["structured_output"] == {"a": 1} and rl == info
    # The plain json output shape.
    data, rl = cli.parse_turn_output([json.dumps({"is_error": False, "result": "x"})])
    assert data["result"] == "x" and rl is None
    with pytest.raises(ValueError):
        cli.parse_turn_output(["not json"])


@pytest.mark.asyncio
async def test_collector_turn_records_usage_for_the_default_account(db):
    out = [json.dumps({"type": "rate_limit_event", "rate_limit_info": _info(seven=0.6)}),
           json.dumps({"type": "result", "is_error": False, "structured_output": {}, "usage": {}})]
    with patch.object(cli, "_run_process", new_callable=AsyncMock, return_value=out), \
         patch.object(cli, "get_agent_binary", return_value="claude"):
        await cli.run_collect("slack", "p", {"type": "object"}, ["t"])
    assert (await accounts.windows_of("default"))["seven_day"]["utilization"] == 0.6


# ── budget: pause only when every account is over ────────────────────────


async def _evaluate():
    cfg = {"enabled": True, "agents": {"claude_code": {"window_token_limit": 0}},
           "pause_at_utilization": T}
    paused = {}

    async def fake_pause(until, reason):
        paused.update(until=until, reason=reason)

    with patch.object(agent_budget, "get_agent_budget_config", return_value=cfg), \
         patch.object(agent_budget, "pause_for_agent_budget", side_effect=fake_pause):
        await agent_budget.evaluate_agent_budget()
    return paused


@pytest.mark.asyncio
async def test_budget_does_not_pause_while_one_account_has_room(db):
    await _add_row("acct1", email="first@example.com")
    await _add_row()
    await accounts.record("acct1", _info(seven=0.9, status="allowed_warning"))
    await accounts.record("acct2", _info(seven=0.3))
    assert await _evaluate() == {}


@pytest.mark.asyncio
async def test_budget_pauses_when_every_account_is_over_until_the_first_recovers(db):
    await _add_row("acct1", email="first@example.com")
    await _add_row()
    soon, later = int(time.time()) + 3600, int(time.time()) + 86400
    await accounts.record("acct1", _info(seven=0.9, status="allowed_warning", resets=later))
    await accounts.record("acct2", _info(five=0.95, status="allowed_warning", resets=soon))
    paused = await _evaluate()
    assert paused["reason"].startswith(accounts.ACCOUNTS_PAUSE_REASON)
    assert paused["until"] == agent_budget.db_ts_from_epoch(soon)


@pytest.mark.asyncio
async def test_single_default_account_keeps_the_old_behaviour(db):
    await accounts.record("default", _info(seven=0.88, status="allowed_warning"))
    assert (await _evaluate())["reason"].startswith(accounts.ACCOUNTS_PAUSE_REASON)
    # An expired window no longer pauses.
    await accounts.record("default", _info(seven=0.88, status="allowed_warning", resets=PAST))
    assert await _evaluate() == {}


@pytest.mark.asyncio
async def test_pause_ends_early_once_an_account_has_room(db):
    until = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE agent_budget_state SET paused_at = CURRENT_TIMESTAMP, paused_until = ?, paused_reason = ? WHERE id = 1",
        (until, accounts.ACCOUNTS_PAUSE_REASON + " (acct1 seven_day)"),
    )
    await db.commit()
    await _add_row("acct1", email="first@example.com")
    await accounts.record("acct1", _info(seven=0.9, status="allowed_warning"))
    resume = AsyncMock()
    with patch.object(agent_budget, "resume_from_agent_budget", resume):
        await agent_budget.maybe_resume_agent_budget()
        resume.assert_not_awaited()
        await _add_row()
        await agent_budget.maybe_resume_agent_budget()
        resume.assert_awaited_once()


# ── sign-in (fake claude in a real PTY) ──────────────────────────────────

URL = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
GOOD = "good-code#abc"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        "printf 'Opening browser to sign in\\r\\nIf the browser did not open, visit: "
        f"\\033]8;;{URL}\\007{URL}\\033]8;;\\007\\r\\nPaste code here if prompted > '\n"
        "read code\n"
        f"if [ \"$code\" = \"{GOOD}\" ]; then echo 'Login successful.'; exit 0; fi\n"
        "echo \"Login failed: That code wasn't accepted.\"; exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(accounts, "get_agent_binary", lambda _agent: str(script))
    monkeypatch.setattr(accounts, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(accounts, "check_connectors", AsyncMock())
    return script


async def _until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_add_account_flow(db, fake_claude, monkeypatch, capsys):
    status = AsyncMock(return_value={"logged_in": True, "email": "second@example.com",
                                     "plan": "pro", "org_name": None})
    monkeypatch.setattr(accounts, "auth_status", status)
    monkeypatch.setattr(accounts, "default_status", AsyncMock(return_value={"email": "kurt@example.com"}))
    flow = await accounts.start_login()
    account_id = flow["account_id"]
    path = accounts.ACCOUNTS_DIR / account_id
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(accounts.ACCOUNTS_DIR.stat().st_mode) == 0o700

    await _until(lambda: accounts._flow.state == "waiting_code")
    assert accounts.flow_status(account_id)["url"] == URL
    # Single flight.
    with pytest.raises(accounts.AccountError) as e:
        await accounts.start_login()
    assert e.value.status == 409

    await accounts.submit_code(account_id, f"  {GOOD}\n")
    await _until(lambda: accounts._flow.state == "done")
    row = await accounts.get_row(account_id)
    assert row["logged_in"] == 1 and row["email"] == "second@example.com" and row["plan"] == "pro"
    assert status.await_args.args[0] == {"CLAUDE_CONFIG_DIR": str(path)}
    # The code is never logged nor returned.
    assert GOOD not in capsys.readouterr().out
    assert GOOD not in json.dumps(accounts.flow_status(account_id))
    assert accounts._flow.secret is None


@pytest.mark.asyncio
async def test_wrong_code_fails_and_forgets_the_new_account(db, fake_claude, monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", AsyncMock(return_value={"logged_in": False}))
    flow = await accounts.start_login()
    await _until(lambda: accounts._flow.state == "waiting_code")
    await accounts.submit_code(flow["account_id"], "bad-code")
    await _until(lambda: accounts._flow.state == "failed")
    assert "wasn't accepted" in accounts._flow.error
    assert "bad-code" not in accounts._flow.error
    assert await accounts.get_row(flow["account_id"]) is None
    assert not (accounts.ACCOUNTS_DIR / flow["account_id"]).exists()


@pytest.mark.asyncio
async def test_code_is_refused_unless_waiting_or_malformed(db, fake_claude):
    flow = await accounts.start_login()
    await _until(lambda: accounts._flow.state == "waiting_code")
    with pytest.raises(accounts.AccountError):
        await accounts.submit_code(flow["account_id"], "bad\x1bcode")
    with pytest.raises(accounts.AccountError) as e:
        await accounts.submit_code("other", GOOD)
    assert e.value.status == 404
    await accounts.cancel(flow["account_id"])
    with pytest.raises(accounts.AccountError) as e:
        await accounts.submit_code(flow["account_id"], GOOD)
    assert e.value.status == 409


@pytest.mark.asyncio
async def test_cancel_kills_the_cli_and_removes_the_new_account(db, fake_claude):
    flow = await accounts.start_login()
    await _until(lambda: accounts._flow.state == "waiting_code")
    proc = accounts._flow.proc.proc
    result = await accounts.cancel(flow["account_id"])
    assert result["state"] == "cancelled"
    assert proc.poll() is not None
    assert await accounts.get_row(flow["account_id"]) is None


@pytest.mark.asyncio
async def test_sign_in_times_out(db, fake_claude, monkeypatch):
    monkeypatch.setattr(accounts, "LOGIN_TIMEOUT", 0.3)
    await accounts.start_login()
    await _until(lambda: accounts._flow.state == "timed_out")
    assert "10 minutes" in accounts._flow.error


@pytest.mark.asyncio
async def test_same_account_as_another_added_one_is_refused(db, fake_claude, monkeypatch):
    await _add_row("acct1", email="first@example.com", org_id="org-1")
    monkeypatch.setattr(accounts, "auth_status", AsyncMock(return_value={
        "logged_in": True, "email": "First@example.com", "plan": "max", "org_name": None, "org_id": "org-1"}))
    run_cli = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    flow = await accounts.start_login()
    await _until(lambda: accounts._flow.state == "waiting_code")
    await accounts.submit_code(flow["account_id"], GOOD)
    await _until(lambda: accounts._flow.state == "failed")
    assert "already one of your logins" in accounts._flow.error
    assert run_cli.await_args.args[0][1:] == ["auth", "logout"]
    assert await accounts.get_row(flow["account_id"]) is None


def test_parse_login_url():
    assert accounts.parse_login_url(f"visit: \x1b]8;;{URL}\x07\x1b[94m{URL}\x1b[39m") == URL
    assert accounts.parse_login_url(f"Open {URL}\r\n> ") == URL
    assert accounts.parse_login_url("nothing yet") is None


# ── removal ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_logs_out_of_servers_and_account_then_deletes(db, monkeypatch):
    path = await _add_row()
    (path / ".claude.json").write_text(json.dumps({"mcpServers": {
        "linear-server": {"type": "http", "url": "https://mcp.linear.app/mcp"},
        "notion": {"type": "http", "url": "https://mcp.notion.com/mcp"}}}))
    await accounts.record("acct2", _info(five=0.2))
    run_cli = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    monkeypatch.setattr(accounts, "get_agent_binary", lambda _a: "claude")
    await accounts.remove("acct2")
    commands = [c.args[0][1:] for c in run_cli.await_args_list]
    assert commands == [["mcp", "logout", "linear-server"], ["mcp", "logout", "notion"], ["auth", "logout"]]
    assert all(c.args[1] == {"CLAUDE_CONFIG_DIR": str(path)} for c in run_cli.await_args_list)
    assert not path.exists()
    assert await accounts.get_row("acct2") is None
    assert await agent_budget.get_rate_limit_raw("claude_code@acct2") is None
    with pytest.raises(accounts.AccountError):
        await accounts.remove("default")


def test_remove_never_deletes_outside_the_accounts_dir(tmp_path):
    outside = tmp_path / "keep"
    outside.mkdir()
    accounts._remove_account_dir(str(outside))
    assert outside.exists()


# ── connectors ───────────────────────────────────────────────────────────

INIT = {
    "mcp_servers": [
        {"name": "claude.ai Slack", "status": "connected", "source": "claudeai"},
        {"name": "claude.ai Gmail", "status": "needs-auth", "source": "claudeai"},
        {"name": "claude.ai Google Drive", "status": "connected", "source": "claudeai"},
        {"name": "linear-server", "status": "needs-auth", "source": "user"},
    ],
    "tools": [
        "mcp__claude_ai_Slack__slack_search_public_and_private",
        "mcp__claude_ai_Slack__slack_read_thread",
        "mcp__claude_ai_Slack__slack_read_channel",
        "mcp__claude_ai_Google_Drive__read_file_content",
    ],
}


def test_connector_matrix(monkeypatch):
    monkeypatch.setattr(accounts, "default_mcp_template",
                        lambda c: ("notion", {"type": "http", "url": "https://mcp.notion.com/mcp"}) if c == "notion" else None)
    m = accounts.connector_matrix(INIT, {"available": True, "login": "kurt-gh"})
    assert m["slack"]["state"] == "connected" and m["slack"]["kind"] == "claude_ai"
    assert m["gmail"]["state"] == "needs_login" and m["gmail"]["kind"] == "claude_ai"
    assert m["drive"]["state"] == "connected" and m["drive"]["server"] == "claude.ai Google Drive"
    assert m["linear"] == {"label": "Linear", "kind": "mcp", "state": "needs_login",
                           "server": "linear-server", "detail": None, "can_add": False}
    assert m["notion"]["state"] == "missing" and m["notion"]["can_add"] is True
    assert m["github"]["state"] == "connected" and m["github"]["detail"] == "kurt-gh"


def test_default_template_copies_only_type_and_url(monkeypatch, tmp_path):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "github": {"type": "stdio", "command": "npx", "env": {"TOKEN": "secret"}},
        "linear-server": {"type": "http", "url": "https://mcp.linear.app/mcp",
                          "headers": {"Authorization": "Bearer secret"}}}}))
    monkeypatch.setattr(accounts, "default_config_file", lambda: cfg)
    assert accounts.default_mcp_template("linear") == (
        "linear-server", {"type": "http", "url": "https://mcp.linear.app/mcp"})
    assert accounts.default_mcp_template("notion") is None


@pytest.mark.asyncio
async def test_add_connector_runs_mcp_add_in_the_account(db, monkeypatch):
    path = await _add_row()
    monkeypatch.setattr(accounts, "default_mcp_template",
                        lambda c: ("linear-server", {"type": "http", "url": "https://mcp.linear.app/mcp"}))
    monkeypatch.setattr(accounts, "get_agent_binary", lambda _a: "claude")
    run_cli = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    monkeypatch.setattr(accounts, "check_connectors", AsyncMock(return_value={"connectors": {}}))
    await accounts.add_connector("acct2", "linear")
    assert run_cli.await_args.args == (
        ["claude", "mcp", "add", "--scope", "user", "--transport", "http",
         "linear-server", "https://mcp.linear.app/mcp"],
        {"CLAUDE_CONFIG_DIR": str(path)},
    )
    with pytest.raises(accounts.AccountError):
        await accounts.add_connector("acct2", "slack")
    with pytest.raises(accounts.AccountError):
        await accounts.add_connector("default", "linear")


@pytest.mark.asyncio
async def test_check_connectors_lists_the_account_and_stores_the_matrix(db, monkeypatch):
    path = await _add_row()
    read_init = AsyncMock(return_value=INIT)
    monkeypatch.setattr(cli, "read_init", read_init)
    monkeypatch.setattr(accounts, "_github_status", AsyncMock(return_value={"available": False}))
    result = await accounts.check_connectors("acct2")
    assert read_init.await_args.kwargs["env"] == {"CLAUDE_CONFIG_DIR": str(path)}
    assert result["connectors"]["slack"]["state"] == "connected"
    assert (await accounts.stored_connectors("acct2"))["connectors"]["slack"]["state"] == "connected"


@pytest.mark.asyncio
async def test_connector_login_needs_a_registered_server(db, fake_claude):
    path = await _add_row()
    with pytest.raises(accounts.AccountError):
        await accounts.start_connector_login("acct2", "linear")
    (path / ".claude.json").write_text(json.dumps({"mcpServers": {
        "linear-server": {"type": "http", "url": "https://mcp.linear.app/mcp"}}}))
    flow = await accounts.start_connector_login("acct2", "linear")
    assert flow["kind"] == "connector" and flow["server"] == "linear-server"
    await _until(lambda: accounts._flow.state == "waiting_code")
    await accounts.submit_code("acct2", GOOD)
    await _until(lambda: accounts._flow.state == "done")


# ── API ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accounts_api(db, monkeypatch):
    from laya.main import app

    await _add_row()
    await accounts.record("acct2", _info(five=0.4, seven=0.5))
    monkeypatch.setattr(accounts, "default_status", AsyncMock(return_value={
        "logged_in": True, "email": "kurt@example.com", "plan": "max", "org_name": None, "org_id": "org-cli"}))
    monkeypatch.setattr(accounts, "refresh_status", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        data = (await client.get("/claude-code/accounts")).json()
        assert [a["id"] for a in data["accounts"] if a["agent_id"] == "claude_code"] == ["default", "acct2"]
        assert data["accounts"][0]["email"] == "kurt@example.com"
        assert data["accounts"][0]["in_use"] is False and data["accounts"][1]["in_use"] is True
        usage = data["accounts"][1]["usage"]
        assert usage["windows"]["five_hour"]["utilization"] == 0.4 and usage["as_of"]
        assert data["current"] == "acct2" and data["login"] is None
        assert (await client.get("/claude-code/accounts/login")).json() == {"login": None}
        assert (await client.get("/claude-code/accounts/acct2/status")).json() == {"login": None}
        assert (await client.post("/claude-code/accounts/acct2/code", json={"code": "x"})).status_code == 404
        assert (await client.delete("/claude-code/accounts/default")).status_code == 422
        assert (await client.delete("/claude-code/accounts/nope")).status_code == 404
        assert (await client.post("/claude-code/accounts/acct2/connectors/slack/add")).status_code == 422
