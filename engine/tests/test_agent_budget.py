# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent inference backend usage-limit budget (DB-backed logic)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from laya.pipeline import agent_budget as ab


async def _insert_audit(db, model_used, in_tok, out_tok, success=1, ts=None):
    if ts is None:
        await db.execute(
            """INSERT INTO audit_log (log_id, step, model_used, input_tokens, output_tokens,
                                      latency_ms, success)
               VALUES (?, 'route', ?, ?, ?, 10, ?)""",
            (f"a_{model_used}_{in_tok}_{success}", model_used, in_tok, out_tok, success),
        )
    else:
        await db.execute(
            """INSERT INTO audit_log (log_id, step, model_used, input_tokens, output_tokens,
                                      latency_ms, success, timestamp)
               VALUES (?, 'route', ?, ?, ?, 10, ?, ?)""",
            (f"a_old_{model_used}_{in_tok}", model_used, in_tok, out_tok, success, ts),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_record_and_read_rate_limit(db):
    info = {"status": "allowed", "resetsAt": 1782541200, "rateLimitType": "five_hour"}
    await ab.record_rate_limit("claude_code", info)
    rl = await ab.get_rate_limit_state("claude_code")
    assert rl == {"status": "allowed", "resets_at": 1782541200, "rate_limit_type": "five_hour", "utilization": None}
    # Upsert overwrites.
    await ab.record_rate_limit("claude_code", {**info, "status": "rejected"})
    assert (await ab.get_rate_limit_state("claude_code"))["status"] == "rejected"
    assert await ab.get_rate_limit_state("pi_cli") is None


@pytest.mark.asyncio
async def test_window_usage_counts_only_recent_successful_agent_calls(db):
    await _insert_audit(db, "agent/claude_code/claude-sonnet-4-6", 100, 50)   # counted
    await _insert_audit(db, "agent/claude_code", 10, 5)                       # counted (no model)
    await _insert_audit(db, "agent/pi_cli/qwen", 999, 999)                    # other agent
    await _insert_audit(db, "anthropic/claude-haiku-4-5", 500, 500)          # not an agent
    await _insert_audit(db, "agent/claude_code/x", 7, 7, success=0)          # failed → excluded
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")
    await _insert_audit(db, "agent/claude_code/y", 1000, 1000, ts=old_ts)     # outside window

    usage = await ab.get_agent_window_usage("claude_code", window_hours=5)
    assert usage["tokens"] == 165  # 150 + 15
    assert usage["calls"] == 2


@pytest.mark.asyncio
async def test_status_reflects_config_and_usage(db):
    await _insert_audit(db, "agent/claude_code/m", 800_000, 200_000)  # 1.0M tokens
    cfg = {"enabled": True, "agents": {"claude_code": {"window_token_limit": 5_000_000, "window_hours": 5, "pause_at_percent": 85}}}
    with patch.object(ab, "get_agent_budget_config", return_value=cfg):
        status = await ab.get_agent_budget_status()
    assert status["enabled"] is True
    agent = next(a for a in status["agents"] if a["agent_id"] == "claude_code")
    assert agent["tokens_used"] == 1_000_000
    assert agent["window_token_limit"] == 5_000_000
    assert agent["percent"] == 20.0
    assert status["is_paused"] is False


@pytest.mark.asyncio
async def test_evaluate_pauses_when_over_limit(db):
    # 900k of a 1M budget at 85% pause threshold → over.
    await _insert_audit(db, "agent/claude_code/m", 600_000, 300_000)
    cfg = {"enabled": True, "agents": {"claude_code": {"window_token_limit": 1_000_000, "window_hours": 5, "pause_at_percent": 85}}}
    paused = {}

    async def _fake_pause(until, reason):
        paused["until"] = until
        paused["reason"] = reason

    with patch.object(ab, "get_agent_budget_config", return_value=cfg), \
         patch.object(ab, "pause_for_agent_budget", side_effect=_fake_pause):
        await ab.evaluate_agent_budget()

    assert "until" in paused and "claude_code" in paused["reason"]


@pytest.mark.asyncio
async def test_maybe_resume_only_after_window_resets(db):
    # Manually mark paused with a future paused_until → should NOT resume yet.
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await db.execute(
        "UPDATE agent_budget_state SET paused_at = CURRENT_TIMESTAMP, paused_until = ? WHERE id = 1",
        (future,),
    )
    await db.commit()
    resumed = {"called": False}

    async def _fake_resume():
        resumed["called"] = True

    with patch.object(ab, "resume_from_agent_budget", side_effect=_fake_resume):
        await ab.maybe_resume_agent_budget()
        assert resumed["called"] is False  # window not reset yet

        # Past paused_until → eligible to resume.
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await db.execute("UPDATE agent_budget_state SET paused_until = ? WHERE id = 1", (past,))
        await db.commit()
        await ab.maybe_resume_agent_budget()
        assert resumed["called"] is True


# ── native warning thresholds ──


def test_native_warning_pauses_only_at_utilization_threshold():
    T = ab.DEFAULT_PAUSE_AT_UTILIZATION
    week = {"status": "allowed_warning", "rate_limit_type": "seven_day"}
    five = {"status": "allowed_warning", "rate_limit_type": "five_hour"}
    assert not ab.native_limit_reached({**week, "utilization": 0.61}, T)
    assert ab.native_limit_reached({**week, "utilization": 0.8}, T)
    assert not ab.native_limit_reached({**five, "utilization": 0.85}, T)
    assert ab.native_limit_reached({**five, "utilization": 0.95}, T)
    # Unknown utilization or window type, and rejections, still pause.
    assert ab.native_limit_reached({**week, "utilization": None}, T)
    assert ab.native_limit_reached({"status": "allowed_warning", "rate_limit_type": "other", "utilization": 0.1}, T)
    assert ab.native_limit_reached({"status": "rejected", "rate_limit_type": "seven_day", "utilization": 0.5}, T)
    assert not ab.native_limit_reached({"status": "allowed"}, T)
    assert not ab.native_limit_reached(None, T)


@pytest.mark.asyncio
async def test_rate_limit_state_carries_utilization(db):
    await ab.record_rate_limit("claude_code", {
        "status": "allowed_warning", "resetsAt": 1782541200,
        "rateLimitType": "seven_day", "utilization": 0.63,
    })
    assert (await ab.get_rate_limit_state("claude_code"))["utilization"] == 0.63


@pytest.mark.asyncio
async def test_early_weekly_warning_does_not_pause(db):
    await ab.record_rate_limit("claude_code", {
        "status": "allowed_warning", "resetsAt": 1782541200,
        "rateLimitType": "seven_day", "utilization": 0.61,
    })
    cfg = {"enabled": True, "agents": {"claude_code": {"window_token_limit": 0}}}
    with patch.object(ab, "get_agent_budget_config", return_value=cfg), \
         patch.object(ab, "pause_for_agent_budget") as pause:
        await ab.evaluate_agent_budget()
    pause.assert_not_called()


@pytest.mark.asyncio
async def test_token_budget_pause_ends_with_the_window_not_the_native_reset(db):
    # Over the token budget while the CLI reports a weekly window resetting in 5 days.
    await _insert_audit(db, "agent/claude_code/m", 600_000, 300_000)
    weekly_reset = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    await ab.record_rate_limit("claude_code", {
        "status": "allowed_warning", "resetsAt": weekly_reset,
        "rateLimitType": "seven_day", "utilization": 0.63,
    })
    cfg = {"enabled": True, "agents": {"claude_code": {"window_token_limit": 1_000_000, "window_hours": 5, "pause_at_percent": 85}}}
    paused = {}

    async def _fake_pause(until, reason):
        paused["until"] = until

    with patch.object(ab, "get_agent_budget_config", return_value=cfg), \
         patch.object(ab, "pause_for_agent_budget", side_effect=_fake_pause):
        await ab.evaluate_agent_budget()

    assert paused["until"][:16] == ab._ts_in_hours(5)[:16]


# ── config ──


def test_config_defaults_and_update(tmp_path, monkeypatch):
    from laya import config

    monkeypatch.setattr(config, "LAYA_CONFIG_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_settings_cache", None)
    cfg = ab.get_agent_budget_config()
    assert cfg["enabled"] is False
    assert cfg["daily_call_limit"] == ab.DEFAULT_DAILY_CALL_LIMIT == 400
    assert cfg["pause_at_utilization"] == {"five_hour": 0.9, "seven_day": 0.8}

    cfg = ab.update_agent_budget_config(
        True, None, daily_call_limit=0, pause_at_utilization={"seven_day": 0.95},
    )
    assert cfg["daily_call_limit"] == 0
    assert cfg["pause_at_utilization"] == {"five_hour": 0.9, "seven_day": 0.95}
    # Leaving the optional fields out keeps them.
    cfg = ab.update_agent_budget_config(False, {})
    assert cfg["daily_call_limit"] == 0
    assert cfg["pause_at_utilization"]["seven_day"] == 0.95


# ── hard daily call cap ──


def _cap_cfg(limit):
    return {"enabled": False, "agents": {}, "daily_call_limit": limit}


@pytest.mark.asyncio
async def test_daily_calls_count_agent_calls_since_local_midnight(db):
    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    await _insert_audit(db, "agent/pi_cli/q", 1, 1, success=0)              # failures count too
    await _insert_audit(db, "anthropic/claude-haiku-4-5", 1, 1)              # not an agent
    yesterday = (ab._local_midnight() - timedelta(minutes=1)).astimezone(timezone.utc)
    await _insert_audit(db, "agent/claude_code/old", 1, 1, ts=yesterday.strftime("%Y-%m-%d %H:%M:%S"))
    assert await ab.get_agent_daily_calls() == 2


@pytest.mark.asyncio
async def test_daily_cap_under_limit_allows_call(db):
    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    with patch.object(ab, "get_agent_budget_config", return_value=_cap_cfg(2)), \
         patch.object(ab, "pause_for_agent_budget") as pause:
        await ab.enforce_daily_call_limit()
    pause.assert_not_called()


@pytest.mark.asyncio
async def test_daily_cap_reached_refuses_and_pauses_until_local_midnight(db):
    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    await _insert_audit(db, "agent/claude_code/n", 2, 2)
    paused = {}

    async def _fake_pause(until, reason):
        paused["until"] = until
        paused["reason"] = reason

    with patch.object(ab, "get_agent_budget_config", return_value=_cap_cfg(2)), \
         patch.object(ab, "pause_for_agent_budget", side_effect=_fake_pause):
        with pytest.raises(ab.AgentCallLimitReached):
            await ab.enforce_daily_call_limit()

    expected = ab._local_midnight(days_ahead=1).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert paused["until"] == expected
    assert "2/2" in paused["reason"]


@pytest.mark.asyncio
async def test_daily_cap_zero_disables(db):
    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    with patch.object(ab, "get_agent_budget_config", return_value=_cap_cfg(0)):
        await ab.enforce_daily_call_limit()


@pytest.mark.asyncio
async def test_refused_agent_call_never_runs_and_is_not_audited(db):
    from laya.llm import agent_backend, client

    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    with patch.object(ab, "get_agent_budget_config", return_value=_cap_cfg(1)), \
         patch.object(ab, "pause_for_agent_budget"), \
         patch.object(client, "_get_model_for_role", return_value="agent/claude_code/claude-haiku-4-5"), \
         patch.object(agent_backend, "_run_agent") as run:
        with pytest.raises(ab.AgentCallLimitReached):
            await client.llm_call(
                role="router", messages=[{"role": "user", "content": "hi"}], step="route",
            )
    run.assert_not_called()
    rows = await db.execute_fetchall("SELECT COUNT(*) AS n FROM audit_log")
    assert rows[0]["n"] == 1


# ── queue while paused / after a refusal ──


async def _pause_state(db):
    await db.execute(
        "UPDATE agent_budget_state SET paused_at = CURRENT_TIMESTAMP, paused_until = ?, "
        "paused_reason = 'test' WHERE id = 1",
        (ab._ts_in_hours(1),),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_queue_claims_nothing_while_paused(db):
    from laya.pipeline import queue as q
    from tests.conftest import insert_test_event

    await insert_test_event(db, "evt_q")
    await q.enqueue_event("evt_q")
    assert await q._fetch_ready_events(10) == ["evt_q"]

    await _pause_state(db)
    assert await q._fetch_ready_events(10) == []


@pytest.mark.asyncio
async def test_refused_event_goes_back_to_queue_without_an_attempt(db):
    from unittest.mock import AsyncMock, MagicMock

    from laya.pipeline import queue as q
    from tests.conftest import insert_test_event

    await insert_test_event(db, "evt_r")
    await q.enqueue_event("evt_r")
    event = MagicMock(event_id="evt_r")
    refusal = ab.AgentCallLimitReached("daily agent call limit reached (400/400)")

    with patch.object(q, "_load_event", AsyncMock(return_value=event)), \
         patch.object(q, "_load_persisted_router_output", AsyncMock(return_value=None)), \
         patch("laya.pipeline.ingest.run_ingest", AsyncMock(return_value=("external", {}))), \
         patch("laya.pipeline.space_resolution.resolve_space", AsyncMock(return_value="default")), \
         patch("laya.pipeline.rules.run_rules", AsyncMock(return_value=(False, None))), \
         patch("laya.pipeline.router.run_router", AsyncMock(side_effect=refusal)), \
         patch("laya.api.websocket.manager.broadcast", AsyncMock()):
        await q.process_event("evt_r")

    rows = await db.execute_fetchall(
        "SELECT processing_status, processing_attempts, last_error, next_retry_at FROM events WHERE event_id = 'evt_r'"
    )
    assert rows[0]["processing_status"] == "queued"
    assert rows[0]["processing_attempts"] == 0
    assert rows[0]["next_retry_at"] is None
    assert rows[0]["last_error"].startswith("AgentCallLimitReached:")


@pytest.mark.asyncio
async def test_resume_requeues_events_that_died_from_budget_refusals(db):
    from tests.conftest import insert_test_event

    for eid, error in [
        ("evt_cap", "AgentCallLimitReached: daily agent call limit reached (400/400)"),
        ("evt_budget", "AgentBudgetRefused: paused"),
        ("evt_other", "RuntimeError: agent exited 1"),
    ]:
        await insert_test_event(db, eid)
        await db.execute(
            "UPDATE events SET processing_status = 'dead', processing_attempts = 3, last_error = ? WHERE event_id = ?",
            (error, eid),
        )
    await db.commit()
    await _pause_state(db)

    result = await ab.resume_from_agent_budget()

    assert result["requeued_events"] == 2
    rows = await db.execute_fetchall(
        "SELECT event_id, processing_status, processing_attempts FROM events ORDER BY event_id"
    )
    status = {r["event_id"]: (r["processing_status"], r["processing_attempts"]) for r in rows}
    assert status == {
        "evt_budget": ("queued", 0), "evt_cap": ("queued", 0), "evt_other": ("dead", 3),
    }
    assert not await ab.is_agent_budget_paused()


# ── REST ──


@pytest.mark.asyncio
async def test_agent_budget_api_reads_and_updates_cap_and_thresholds(db, tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from laya import config
    from laya.main import app

    monkeypatch.setattr(config, "LAYA_CONFIG_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_settings_cache", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/agent-budget", json={
            "enabled": False, "daily_call_limit": 250,
            "pause_at_utilization": {"five_hour": 0.95, "seven_day": 0.85},
        })
        assert resp.status_code == 200
        status = (await client.get("/agent-budget")).json()
        assert status["daily_call_limit"] == 250
        assert status["daily_calls"] == 0
        assert status["pause_at_utilization"] == {"five_hour": 0.95, "seven_day": 0.85}
        assert status["is_paused"] is False and status["paused_at"] is None

        bad = [{"daily_call_limit": -1}, {"daily_call_limit": "x"},
               {"pause_at_utilization": {"seven_day": 80}}]
        for body in bad:
            assert (await client.put("/agent-budget", json={"enabled": False, **body})).status_code == 422
