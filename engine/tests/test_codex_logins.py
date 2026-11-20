# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Codex logins (claude_code/codex_accounts.py): app-server parsing (captured
responses), the device-code sign-in against a fake `codex` in a real PTY,
eligibility, routing and failover in the agent backend, and the Access API."""

from __future__ import annotations

import asyncio
import json
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import accounts, codex_accounts
from laya.llm import agent_backend as ab
from laya.pipeline import agent_budget

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
FUTURE = int(time.time()) + 3 * 86400


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(codex_accounts, "_route", codex_accounts._Route())
    monkeypatch.setattr(codex_accounts, "_last_poll", {})
    monkeypatch.setattr(accounts, "_flow", None)
    monkeypatch.setattr(accounts, "_thresholds", lambda: {"five_hour": 0.9, "seven_day": 0.8})
    monkeypatch.setattr(codex_accounts, "_default_status", (time.monotonic(), {
        "logged_in": True, "email": "cli@example.com", "plan": "pro", "account_id": "acct-cli", "auth": "chatgpt"}))
    yield
    flow = accounts._flow
    if flow is not None and flow.proc is not None and hasattr(flow.proc, "proc"):
        if flow.proc.proc.poll() is None:
            flow.proc.proc.kill()
            flow.proc.proc.wait()


async def _add_row(login_id: str, email: str, acct: str, logged_in: int = 1) -> None:
    db = await accounts._db()
    home = codex_accounts.HOMES_DIR / login_id
    home.mkdir(parents=True, exist_ok=True)
    await db.execute(
        "INSERT INTO claude_code_accounts (id, config_dir, email, logged_in, created_at, org_id, agent_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'codex_cli')",
        (login_id, str(home), email, logged_in, f"2026-11-20T09:0{len(login_id) % 10}:00", acct),
    )
    await db.commit()


# ── app-server parsing ───────────────────────────────────────────────────


def test_parse_account_fixture():
    resp = json.loads((FIXTURES / "app_server_account_read.json").read_text())
    assert codex_accounts.parse_account(resp) == {
        "logged_in": True, "email": "me@example.com", "plan": "pro", "account_id": "acct-1", "auth": "chatgpt"}
    assert codex_accounts.parse_account({"id": 1, "result": {"account": None}})["logged_in"] is False


def test_rate_limits_fixture_is_a_rejected_week():
    resp = json.loads((FIXTURES / "app_server_rate_limits_reached.json").read_text())
    info = codex_accounts.rate_limit_info(resp)
    assert info["status"] == "rejected" and info["rateLimitType"] == "seven_day"
    assert info["resetsAt"] == 1795166047
    windows = agent_budget.rate_limit_windows(info, now=1795166047 - 60)
    assert windows["seven_day"] == {"utilization": 1.0, "resets_at": 1795166047, "status": "rejected"}


def test_rate_limits_with_room():
    resp = {"result": {"rateLimits": {
        "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": FUTURE},
        "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": FUTURE},
        "rateLimitReachedType": None}, "ordinaryUsageAllowed": True}}
    info = codex_accounts.rate_limit_info(resp)
    assert info["status"] == "allowed"
    assert info["unifiedWindows"]["five_hour"]["utilization"] == 0.12
    assert info["unifiedWindows"]["seven_day"]["utilization"] == 0.4


def test_device_code_from_captured_output():
    text = (FIXTURES / "device_auth_output.txt").read_text()
    assert codex_accounts.parse_device_code(text) == "ABCD-12EFG"
    assert accounts.parse_login_url(text) == "https://auth.openai.com/codex/device"


# ── eligibility and routing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_login_only_while_none_added(db):
    assert await codex_accounts.eligible_ids() == [codex_accounts.DEFAULT_ID]
    await _add_row("c1", "one@example.com", "acct-1")
    assert await codex_accounts.eligible_ids() == ["c1"]
    # Claude's own list never shows Codex rows.
    assert await accounts.list_rows() == []
    assert await accounts.agent_of("c1") == "codex_cli"


@pytest.mark.asyncio
async def test_duplicates_and_signed_out_are_not_eligible(db):
    await _add_row("c1", "one@example.com", "acct-1")
    await _add_row("c22", "dup@example.com", "acct-1")
    await _add_row("c333", "out@example.com", "acct-3", logged_in=0)
    assert await codex_accounts.eligible_ids() == ["c1"]


@pytest.mark.asyncio
async def test_pick_and_failover_on_limit(db):
    await _add_row("c1", "one@example.com", "acct-1")
    await _add_row("c22", "two@example.com", "acct-2")
    login, env = await codex_accounts.pick()
    assert login == "c1" and env == {"CODEX_HOME": str(codex_accounts.HOMES_DIR / "c1")}
    await codex_accounts.record("c1", codex_accounts.limit_info("You've hit your usage limit. try again at 10:14 AM"))
    nxt = await codex_accounts.failover("c1")
    assert nxt[0] == "c22"
    blocked, _, _ = await codex_accounts.native_block({"five_hour": 0.9, "seven_day": 0.8})
    assert blocked is False


@pytest.mark.asyncio
async def test_backend_runs_on_the_login_and_fails_over(db, monkeypatch):
    await _add_row("c1", "one@example.com", "acct-1")
    await _add_row("c22", "two@example.com", "acct-2")
    limit = (FIXTURES / "exec_usage_limit.jsonl").read_text().splitlines()
    ok = (FIXTURES / "exec_success_schema.jsonl").read_text().splitlines()
    scripts = [limit, ok]
    envs: list = []

    class Proc:
        stderr_output = ""

        async def spawn(self, args, cwd=None, env=None):
            envs.append(env)
            self.lines = scripts.pop(0)

        async def read_lines(self, idle_timeout=0):
            for line in self.lines:
                yield line

        async def wait(self):
            return 0

        async def terminate(self):
            pass

    monkeypatch.setattr(ab, "AgentProcess", Proc)
    result = await ab._run_agent_routed("codex_cli", "codex", "s", "u", None, None, True, 30.0)
    assert [e["CODEX_HOME"] for e in envs] == [str(codex_accounts.HOMES_DIR / n) for n in ("c1", "c22")]
    assert result["account_id"] == "c22"
    raw = await agent_budget.get_rate_limit_raw("codex_cli@c1")
    assert raw["status"] == "rejected"


@pytest.mark.asyncio
async def test_cli_login_row_is_keyed_by_chatgpt_account(db):
    assert await codex_accounts.rate_row(codex_accounts.DEFAULT_ID) == "codex_cli@acct:acct-cli"


# ── sign-in (fake codex in a real PTY) ───────────────────────────────────


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/bin/sh\n"
        "printf 'Follow these steps to sign in with ChatGPT using device code authorization:\\r\\n\\r\\n"
        "1. Open this link in your browser and sign in to your account\\r\\n"
        "   \\033[94mhttps://auth.openai.com/codex/device\\033[0m\\r\\n\\r\\n"
        "2. Enter this one-time code \\033[90m(expires in 15 minutes)\\033[0m\\r\\n"
        "   \\033[94mWXYZ-98765\\033[0m\\r\\n'\n"
        "if [ -f \"$CODEX_HOME/approve\" ]; then exit 0; fi\n"
        "sleep 30\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(codex_accounts, "get_agent_binary", lambda _a: str(script))
    monkeypatch.setattr(accounts, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(codex_accounts, "poll_usage", AsyncMock())
    return script


async def _until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_device_sign_in_shows_code_and_cancel_cleans_up(db, fake_codex):
    flow = await codex_accounts.start_login()
    login_id = flow["account_id"]
    home = codex_accounts.HOMES_DIR / login_id
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    await _until(lambda: accounts._flow.state == "waiting_code")
    state = accounts.login_state()
    assert state["agent_id"] == "codex_cli"
    assert state["url"] == "https://auth.openai.com/codex/device"
    assert state["user_code"] == "WXYZ-98765"
    # One sign-in at a time, across agents.
    with pytest.raises(accounts.AccountError):
        await accounts.start_login()
    await accounts.cancel(login_id)
    assert accounts._flow.state == "cancelled"
    assert await codex_accounts.get_row(login_id) is None
    assert not home.exists()


@pytest.mark.asyncio
async def test_device_sign_in_success_and_duplicate(db, fake_codex, monkeypatch, tmp_path):
    status = AsyncMock(return_value={"logged_in": True, "email": "new@example.com", "plan": "plus",
                                     "account_id": "acct-9", "auth": "chatgpt"})
    monkeypatch.setattr(codex_accounts, "account_status", status)
    real_new_home = codex_accounts._new_home

    def approving_home(login_id):
        path = real_new_home(login_id)
        (path / "approve").write_text("")
        return path

    monkeypatch.setattr(codex_accounts, "_new_home", approving_home)
    flow = await codex_accounts.start_login()
    await _until(lambda: accounts._flow.state == "done")
    row = await codex_accounts.get_row(flow["account_id"])
    assert row["logged_in"] == 1 and row["email"] == "new@example.com" and row["org_id"] == "acct-9"
    assert status.await_args.args[0] == {"CODEX_HOME": row["config_dir"]}

    run_cli = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(accounts, "_run_cli", run_cli)
    second = await codex_accounts.start_login()
    await _until(lambda: accounts._flow.state == "failed")
    assert "already one of your Codex logins" in accounts._flow.error
    assert run_cli.await_args.args[0][1:] == ["logout"]
    assert await codex_accounts.get_row(second["account_id"]) is None


# ── overview and API ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_lists_codex_logins_with_their_agent(db, monkeypatch):
    monkeypatch.setattr(accounts, "default_status", AsyncMock(return_value={
        "logged_in": True, "email": "kurt@example.com", "plan": "max", "org_name": None, "org_id": "org-1"}))
    await _add_row("c1", "one@example.com", "acct-1")
    await codex_accounts.record("c1", codex_accounts.limit_info("usage limit; try again in 2 hours"))
    out = await accounts.overview()
    by_id = {a["id"]: a for a in out["accounts"]}
    assert by_id["default"]["agent_id"] == "claude_code"
    assert by_id["codex-default"]["agent_id"] == "codex_cli" and by_id["codex-default"]["in_use"] is False
    c1 = by_id["c1"]
    assert c1["agent_id"] == "codex_cli" and c1["in_use"] is True
    assert any(w["status"] == "rejected" for w in c1["usage"]["windows"].values())
    assert {a["agent_id"] for a in out["agents"]} == {"claude_code", "codex_cli"}


@pytest.mark.asyncio
async def test_api_adds_a_codex_login(db, fake_codex):
    from laya.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/claude-code/accounts", json={"agent": "codex_cli"})
        assert r.status_code == 200 and r.json()["agent_id"] == "codex_cli"
        login_id = r.json()["account_id"]
        await _until(lambda: accounts._flow.state == "waiting_code")
        r = await client.post(f"/claude-code/accounts/{login_id}/cancel")
        assert r.json()["state"] == "cancelled"
        r = await client.post("/claude-code/accounts", json={"agent": "gemini_cli"})
        assert r.status_code == 422
