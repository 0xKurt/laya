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
    assert rl == {"status": "allowed", "resets_at": 1782541200, "rate_limit_type": "five_hour"}
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
    cfg = {"enabled": True, "daily_call_limit": 400, "agents": {"claude_code": {"window_token_limit": 5_000_000, "window_hours": 5, "pause_at_percent": 85}}}
    with patch.object(ab, "get_agent_budget_config", return_value=cfg):
        status = await ab.get_agent_budget_status()
    assert status["enabled"] is True
    agent = next(a for a in status["agents"] if a["agent_id"] == "claude_code")
    assert agent["tokens_used"] == 1_000_000
    assert agent["window_token_limit"] == 5_000_000
    assert agent["percent"] == 20.0
    assert status["is_paused"] is False
    assert status["daily_calls"] == 1
    assert status["daily_call_limit"] == 400


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


# ── defaults + hard daily call cap ──


def test_defaults_enable_budget_with_claude_code_cap(tmp_path, monkeypatch):
    """A settings.json without agent_budgets gets the enabled default and caps."""
    import json

    from laya import config

    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"coding_agent": "claude_code"}))
    monkeypatch.setattr(config, "LAYA_CONFIG_FILE", f)
    monkeypatch.setattr(config, "_settings_cache", None)
    cfg = ab.get_agent_budget_config()
    assert cfg["enabled"] is True
    assert cfg["daily_call_limit"] == 400
    assert cfg["agents"]["claude_code"]["window_token_limit"] == 2_000_000

    # A file that has agent_budgets but no daily_call_limit still gets the cap.
    f.write_text(json.dumps({"agent_budgets": {"enabled": False, "agents": {}}}))
    cfg = ab.get_agent_budget_config()
    assert cfg["daily_call_limit"] == 400


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
async def test_daily_cap_reached_refuses_and_pauses_until_midnight(db):
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

    assert paused["until"] == ab._local_midnight(days_ahead=1).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert "2/2" in paused["reason"]


@pytest.mark.asyncio
async def test_daily_cap_zero_disables(db):
    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    with patch.object(ab, "get_agent_budget_config", return_value=_cap_cfg(0)):
        await ab.enforce_daily_call_limit()


@pytest.mark.asyncio
async def test_agent_llm_call_refused_when_cap_reached(db):
    from laya.llm import agent_backend

    await _insert_audit(db, "agent/claude_code/m", 1, 1)
    with patch.object(ab, "get_agent_budget_config", return_value=_cap_cfg(1)), \
         patch.object(ab, "pause_for_agent_budget"), \
         patch.object(agent_backend, "_run_agent") as run:
        with pytest.raises(ab.AgentCallLimitReached):
            await agent_backend.agent_llm_call("agent/claude_code/claude-haiku-4-5", [{"role": "user", "content": "hi"}])
    run.assert_not_called()
