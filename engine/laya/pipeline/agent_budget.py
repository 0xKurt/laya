# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Usage-limit budgeting for the agent inference backend.

Agents (Claude Code, Codex, …) don't bill in dollars — they bill against time-windowed
usage limits that reset (Claude: a rolling 5-hour window). So this is a parallel to the
monthly $ budget (``pipeline/budget.py``) but **window-based with auto-resume**:

- Per-agent config (``settings.agent_budgets.agents[agent_id]``): a token budget over a
  rolling window. We sum the agent's in+out tokens from ``audit_log`` (where agent calls
  are logged as ``model_used='agent/<id>/…'``) over the last ``window_hours``.
- Native signal (Claude Code only): we scrape ``rate_limit_event.rate_limit_info`` from the
  CLI (status + resetsAt) and treat it as authoritative when present.
- When an agent crosses ``pause_at_percent`` of its window budget (or its native limit is
  hit), we pause ingestion (same n8n lever the $ budget uses) until the window resets, then
  the scheduler auto-resumes. This is the user-chosen "defer + auto-resume at reset".
  A native ``allowed_warning`` pauses only once the window's utilization reaches
  ``pause_at_utilization`` for its window type; a rejection always pauses.
- A hard daily cap (``daily_call_limit``) on agent inference calls, applied whether or not
  the window budgets are enabled. Reaching it refuses further calls and pauses ingestion
  until local midnight.
- While paused, the event queue claims nothing (``pipeline/queue.py``). An event whose
  agent call the budget refuses goes back to the queue; resuming re-queues events that
  died from such refusals.
"""

from __future__ import annotations

import json
import time as time_module
from datetime import date, datetime, time, timedelta, timezone

import structlog

from laya.config import load_settings, save_settings
from laya.db.sqlite import get_db
from laya.db.timeutil import db_ts, db_ts_from_epoch

log = structlog.get_logger()

DEFAULT_PAUSE_AT_PERCENT = 85
DEFAULT_WINDOW_HOURS = 5.0
DEFAULT_DAILY_CALL_LIMIT = 400

# Native rate-limit statuses we consider "still OK to use".
_OK_STATUSES = {"allowed", None, ""}

# Claude Code reports "allowed_warning" early (e.g. at 61% of the weekly window, days
# before its reset); pausing on it would stop ingestion while the user still has room.
# A warning pauses only once the utilization of its window type reaches this fraction.
DEFAULT_PAUSE_AT_UTILIZATION = {"five_hour": 0.9, "seven_day": 0.8}


class AgentBudgetRefused(RuntimeError):
    """An agent inference call the agent budget refused to run."""


class AgentCallLimitReached(AgentBudgetRefused):
    """The daily cap on agent inference calls is reached."""


# ── config ───────────────────────────────────────────────────────────────


def get_agent_budget_config() -> dict:
    """Read agent-budget config from settings.json, with this module's defaults."""
    s = load_settings().get("agent_budgets", {}) or {}
    limit = s.get("daily_call_limit")
    return {
        "enabled": bool(s.get("enabled", False)),
        "agents": s.get("agents", {}) or {},
        "daily_call_limit": DEFAULT_DAILY_CALL_LIMIT if limit is None else max(0, int(limit)),
        "pause_at_utilization": {
            **DEFAULT_PAUSE_AT_UTILIZATION, **(s.get("pause_at_utilization") or {})
        },
    }


def update_agent_budget_config(
    enabled: bool,
    agents: dict | None,
    daily_call_limit: int | None = None,
    pause_at_utilization: dict | None = None,
) -> dict:
    """Persist agent-budget config to settings.json. None leaves a field unchanged."""
    s = load_settings()
    ab = s.get("agent_budgets", {}) or {}
    ab["enabled"] = bool(enabled)
    if agents is not None:
        ab["agents"] = agents
    if daily_call_limit is not None:
        ab["daily_call_limit"] = max(0, int(daily_call_limit))
    if pause_at_utilization is not None:
        ab["pause_at_utilization"] = {
            k: min(1.0, max(0.0, float(v))) for k, v in pause_at_utilization.items()
        }
    s["agent_budgets"] = ab
    save_settings(s)
    return get_agent_budget_config()


# ── time helpers ─────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _audit_window_start(window_hours: float) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' for the start of the rolling window (matches audit_log)."""
    return (_now_utc() - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S")


def _ts_in_hours(hours: float) -> str:
    return db_ts(_now_utc() + timedelta(hours=hours))


def _local_midnight(days_ahead: int = 0) -> datetime:
    """Local midnight (today, or ``days_ahead`` days later) as an aware datetime.
    ``astimezone()`` reads a naive datetime as local time, DST included."""
    return datetime.combine(date.today() + timedelta(days=days_ahead), time.min).astimezone()


def _ts_from_unix(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return db_ts_from_epoch(int(ts))
    except (ValueError, OverflowError, OSError):
        return None


# ── rate-limit signal (Claude Code) ──────────────────────────────────────


async def record_rate_limit(agent_id: str, info: dict) -> None:
    """Upsert the latest native rate-limit signal for an agent. Never raises."""
    if not info:
        return
    try:
        db = await get_db()
        await db.execute(
            """INSERT INTO agent_rate_limit_state
                   (agent_id, status, resets_at, rate_limit_type, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(agent_id) DO UPDATE SET
                   status=excluded.status, resets_at=excluded.resets_at,
                   rate_limit_type=excluded.rate_limit_type, raw_json=excluded.raw_json,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                agent_id,
                info.get("status"),
                info.get("resetsAt"),
                info.get("rateLimitType"),
                json.dumps(info),
            ),
        )
        await db.commit()
    except Exception as e:
        log.warning("agent_rate_limit_record_failed", agent_id=agent_id, error=str(e))


async def get_rate_limit_state(agent_id: str) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT status, resets_at, rate_limit_type, raw_json FROM agent_rate_limit_state WHERE agent_id = ?",
        (agent_id,),
    )
    if not rows:
        return None
    r = rows[0]
    try:
        raw = json.loads(r["raw_json"] or "{}")
    except (TypeError, ValueError):
        raw = {}
    return {
        "status": r["status"],
        "resets_at": r["resets_at"],
        "rate_limit_type": r["rate_limit_type"],
        "utilization": raw.get("utilization") if isinstance(raw, dict) else None,
    }


async def get_rate_limit_raw(agent_id: str) -> dict | None:
    """The last ``rate_limit_info`` recorded for an agent row, as the CLI sent it."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT raw_json FROM agent_rate_limit_state WHERE agent_id = ?", (agent_id,)
    )
    if not rows:
        return None
    try:
        raw = json.loads(rows[0]["raw_json"] or "{}")
    except (TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def rate_limit_windows(raw: dict, now: float | None = None) -> dict[str, dict]:
    """Per-window usage from a ``rate_limit_info``:
    ``{window type: {utilization, resets_at, status}}``.

    Claude Code reports every window it tracks in ``unifiedWindows``
    (``five_hour``, ``seven_day``); the top-level fields describe the window
    that triggered the event and fill in a window missing there. A status
    without any window type is kept under ``"unknown"``. A window whose
    ``resetsAt`` has passed has reset: utilization 0, status ``allowed``.
    """
    now = time_module.time() if now is None else now
    windows: dict[str, dict] = {}
    unified = raw.get("unifiedWindows") if isinstance(raw, dict) else None
    if isinstance(unified, dict):
        for name, w in unified.items():
            if isinstance(w, dict):
                windows[str(name)] = {
                    "utilization": w.get("utilization"),
                    "resets_at": w.get("resetsAt"),
                    "status": w.get("status"),
                }
    top_type = raw.get("rateLimitType") if isinstance(raw, dict) else None
    if raw and raw.get("status") and (top_type or not windows):
        windows.setdefault(str(top_type or "unknown"), {
            "utilization": raw.get("utilization"),
            "resets_at": raw.get("resetsAt"),
            "status": raw.get("status"),
        })
    if raw and raw.get("status") == "rejected":
        # A rejection blocks the account even when the window list still shows
        # the state from before it.
        w = windows.setdefault(str(top_type or "unknown"), {"utilization": None})
        w["status"] = "rejected"
        w["resets_at"] = raw.get("resetsAt") or w.get("resets_at")
    for w in windows.values():
        try:
            reset = float(w["resets_at"]) if w.get("resets_at") else None
        except (TypeError, ValueError):
            reset = None
        if reset is not None and reset <= now:
            w.update(utilization=0.0, status="allowed")
    return windows


def native_limit_reached(rl: dict | None, thresholds: dict) -> bool:
    """Whether the CLI's own rate-limit signal should pause ingestion.

    A rejection (or any status other than allowed / allowed_warning) pauses: the agent
    cannot run. A warning pauses once its window's utilization reaches the threshold
    for that window type, or when the utilization or threshold is unknown.
    """
    if not rl or rl.get("status") in _OK_STATUSES:
        return False
    if rl.get("status") != "allowed_warning":
        return True
    threshold = thresholds.get(rl.get("rate_limit_type") or "")
    utilization = rl.get("utilization")
    if threshold is None or utilization is None:
        return True
    try:
        return float(utilization) >= float(threshold)
    except (TypeError, ValueError):
        return True


# ── usage accounting ─────────────────────────────────────────────────────


async def get_agent_window_usage(agent_id: str, window_hours: float) -> dict:
    """Tokens (in+out) consumed by an agent in the rolling window, from audit_log."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS toks,
                  COUNT(*) AS n
           FROM audit_log
           WHERE (model_used = ? OR model_used LIKE ?)
             AND timestamp > ? AND success = 1""",
        (f"agent/{agent_id}", f"agent/{agent_id}/%", _audit_window_start(window_hours)),
    )
    row = rows[0] if rows else None
    return {"tokens": (row["toks"] if row else 0) or 0, "calls": (row["n"] if row else 0) or 0}


async def get_agent_daily_calls() -> int:
    """Agent inference calls (any agent, successful or not) since local midnight."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM audit_log WHERE model_used LIKE 'agent/%' AND timestamp >= ?",
        (db_ts(_local_midnight()),),
    )
    return (rows[0]["n"] if rows else 0) or 0


async def enforce_daily_call_limit() -> None:
    """Refuse an agent call once today's cap is reached, pausing ingestion until local
    midnight. Raises AgentCallLimitReached; a cap of 0 turns the check off."""
    limit = get_agent_budget_config().get("daily_call_limit", DEFAULT_DAILY_CALL_LIMIT)
    if limit <= 0:
        return
    calls = await get_agent_daily_calls()
    if calls < limit:
        return
    reason = f"daily agent call limit reached ({calls}/{limit})"
    await pause_for_agent_budget(db_ts(_local_midnight(days_ahead=1)), reason)
    raise AgentCallLimitReached(f"{reason}; agent calls resume at local midnight")


# ── pause state ──────────────────────────────────────────────────────────


async def _get_state() -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT paused_at, paused_until, paused_reason FROM agent_budget_state WHERE id = 1"
    )
    if not rows:
        return {"paused_at": None, "paused_until": None, "paused_reason": None}
    r = rows[0]
    return {
        "paused_at": r["paused_at"],
        "paused_until": r["paused_until"],
        "paused_reason": r["paused_reason"],
    }


async def is_agent_budget_paused() -> bool:
    return (await _get_state())["paused_at"] is not None


async def get_paused_workflow_count() -> int:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) AS cnt FROM agent_budget_paused_workflows")
    return rows[0]["cnt"] if rows else 0


# ── status (for API / settings panel / footer) ───────────────────────────


async def get_agent_budget_status() -> dict:
    """Per-agent window usage + limits + native rate-limit + pause state."""
    cfg = get_agent_budget_config()
    state = await _get_state()

    agents_status = []
    for agent_id, ac in cfg["agents"].items():
        window_hours = float(ac.get("window_hours") or DEFAULT_WINDOW_HOURS)
        limit = int(ac.get("window_token_limit") or 0)
        usage = await get_agent_window_usage(agent_id, window_hours)
        rl = await get_rate_limit_state(agent_id)
        percent = round(100.0 * usage["tokens"] / limit, 1) if limit > 0 else None
        agents_status.append({
            "agent_id": agent_id,
            "window_hours": window_hours,
            "window_token_limit": limit,
            "pause_at_percent": int(ac.get("pause_at_percent") or DEFAULT_PAUSE_AT_PERCENT),
            "tokens_used": usage["tokens"],
            "calls": usage["calls"],
            "percent": percent,
            "rate_limit": rl,
        })

    return {
        "enabled": cfg["enabled"],
        "agents": agents_status,
        "daily_calls": await get_agent_daily_calls(),
        "daily_call_limit": cfg.get("daily_call_limit", DEFAULT_DAILY_CALL_LIMIT),
        "pause_at_utilization": cfg.get("pause_at_utilization", DEFAULT_PAUSE_AT_UTILIZATION),
        "is_paused": state["paused_at"] is not None,
        "paused_at": state["paused_at"],
        "paused_until": state["paused_until"],
        "paused_reason": state["paused_reason"],
        "paused_workflow_count": await get_paused_workflow_count() if state["paused_at"] else 0,
    }


# ── evaluate / pause / resume ─────────────────────────────────────────────


async def evaluate_agent_budget() -> None:
    """Pause ingestion if any configured agent is over its window/native limit.

    Designed to be called fire-and-forget after an agent llm_call (like check_budget).
    """
    cfg = get_agent_budget_config()
    if not cfg["enabled"] or not cfg["agents"]:
        return
    if (await _get_state())["paused_at"] is not None:
        return  # already paused; the scheduler handles resume

    recovery_times: list[str] = []
    reasons: list[str] = []
    for agent_id, ac in cfg["agents"].items():
        window_hours = float(ac.get("window_hours") or DEFAULT_WINDOW_HOURS)
        limit = int(ac.get("window_token_limit") or 0)
        pause_pct = int(ac.get("pause_at_percent") or DEFAULT_PAUSE_AT_PERCENT)
        thresholds = cfg.get("pause_at_utilization", DEFAULT_PAUSE_AT_UTILIZATION)

        # Native signal first — when the CLI itself reports we're no longer allowed.
        if agent_id == "claude_code":
            # Claude Code spreads its turns over the user's Claude accounts
            # (claude_code/accounts.py); it is blocked only when all of them are.
            from laya.claude_code import accounts

            blocked, until, why = await accounts.native_block(thresholds)
            if blocked:
                recovery_times.append(until or _ts_in_hours(window_hours))
                reasons.append(why)
                continue
        else:
            rl = await get_rate_limit_state(agent_id)
            if native_limit_reached(rl, thresholds):
                recovery_times.append(_ts_from_unix(rl.get("resets_at")) or _ts_in_hours(window_hours))
                reasons.append(f"{agent_id}: rate limit {rl['status']}")
                continue

        # Token-window budget.
        if limit > 0:
            usage = await get_agent_window_usage(agent_id, window_hours)
            if usage["tokens"] >= (pause_pct / 100.0) * limit:
                # The rolling token window has fully turned over window_hours from now.
                # The native reset belongs to the CLI's own window (often the weekly
                # one) and would hold the pause for days over a budget that frees up
                # within hours.
                recovery_times.append(_ts_in_hours(window_hours))
                reasons.append(f"{agent_id}: {usage['tokens']}/{limit} tokens in {window_hours}h")

    if recovery_times:
        # Pause until the latest offender recovers, so a single resume clears all of them.
        await pause_for_agent_budget(max(recovery_times), "; ".join(reasons))


async def pause_for_agent_budget(paused_until: str, reason: str) -> dict:
    """Deactivate all ingestion workflows until an agent's usage window resets."""
    from laya.integrations.n8n_client import activate_workflow, N8nApiError, N8nApiKeyMissing

    db = await get_db()
    if await is_agent_budget_paused():
        return {"already_paused": True}

    source_rows = await db.execute_fetchall(
        "SELECT source_id, workflow_id, space_id FROM sources WHERE source_type = 'ingestion'"
    )
    paused, errors = [], []
    for sr in source_rows:
        wf_id = sr["workflow_id"]
        try:
            await activate_workflow(wf_id, active=False)
            await db.execute(
                "INSERT OR IGNORE INTO agent_budget_paused_workflows (workflow_id, source_id, space_id) VALUES (?, ?, ?)",
                (wf_id, sr["source_id"], sr["space_id"]),
            )
            paused.append(wf_id)
        except (N8nApiError, N8nApiKeyMissing) as e:
            errors.append({"workflow_id": wf_id, "error": str(e)})

    await db.execute(
        "UPDATE agent_budget_state SET paused_at = CURRENT_TIMESTAMP, paused_until = ?, paused_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (paused_until, reason),
    )
    await db.commit()
    log.warning("agent_budget_paused", reason=reason, paused_until=paused_until, paused_count=len(paused))
    await _broadcast_status(paused=True)
    return {"paused_count": len(paused), "errors": errors}


async def resume_from_agent_budget() -> dict:
    """Reactivate workflows paused by an agent-usage pause and clear the pause state."""
    from laya.integrations.n8n_client import (
        activate_workflow,
        check_workflow_readiness,
        N8nApiError,
        N8nApiKeyMissing,
    )

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT workflow_id, source_id, space_id FROM agent_budget_paused_workflows"
    )

    resumed, errors = [], []
    for row in rows:
        wf_id = row["workflow_id"]
        try:
            readiness = await check_workflow_readiness(wf_id)
            if not readiness["ready"]:
                errors.append({"workflow_id": wf_id, "issues": readiness["issues"]})
                continue
            await activate_workflow(wf_id, active=True)
            resumed.append(wf_id)
        except (N8nApiError, N8nApiKeyMissing) as e:
            errors.append({"workflow_id": wf_id, "error": str(e)})

    await db.execute("DELETE FROM agent_budget_paused_workflows")
    await db.execute(
        "UPDATE agent_budget_state SET paused_at = NULL, paused_until = NULL, paused_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = 1"
    )
    await db.commit()
    requeued = await requeue_budget_refused_events()
    log.info(
        "agent_budget_resumed", resumed_count=len(resumed), errors=len(errors),
        requeued_events=requeued,
    )
    await _broadcast_status(paused=False)
    return {"resumed_count": len(resumed), "errors": errors, "requeued_events": requeued}


# The queue stores an event's error as "<ExceptionClass>: <message>".
_REFUSAL_ERROR_PREFIXES = (f"{AgentBudgetRefused.__name__}:", f"{AgentCallLimitReached.__name__}:")


async def requeue_budget_refused_events() -> int:
    """Put events that died from a budget refusal back on the queue with a fresh retry
    budget. They never failed on their own content, so they are not manual retries."""
    db = await get_db()
    conditions = " OR ".join("last_error LIKE ?" for _ in _REFUSAL_ERROR_PREFIXES)
    cursor = await db.execute(
        f"""UPDATE events
               SET processing_status = 'queued', processing_attempts = 0,
                   last_error = NULL, next_retry_at = NULL
             WHERE processing_status = 'dead' AND ({conditions})""",
        tuple(f"{p}%" for p in _REFUSAL_ERROR_PREFIXES),
    )
    await db.commit()
    return cursor.rowcount or 0


async def maybe_resume_agent_budget() -> None:
    """Scheduler tick: auto-resume once the agent's usage window has reset."""
    state = await _get_state()
    if not state["paused_at"]:
        return
    paused_until = state["paused_until"]
    if paused_until:
        try:
            pu = datetime.fromisoformat(paused_until)
            if pu.tzinfo is None:
                pu = pu.replace(tzinfo=timezone.utc)
        except ValueError:
            pu = _now_utc()  # malformed → eligible to resume
        if _now_utc() < pu and not await _claude_accounts_have_room(state["paused_reason"]):
            return  # window hasn't reset yet
    log.info("agent_budget_auto_resume", reason="window_reset")
    await resume_from_agent_budget()


async def _claude_accounts_have_room(reason: str | None) -> bool:
    """Whether a pause that only waits for Claude accounts to recover can end
    early, because an account has room again (e.g. one was just added)."""
    from laya.claude_code import accounts

    # Reasons of several agents are joined with "; "; only a pause with the
    # accounts reason alone waits for nothing but the accounts.
    if not reason or not reason.startswith(accounts.ACCOUNTS_PAUSE_REASON) or "; " in reason:
        return False
    try:
        blocked, _, _ = await accounts.native_block(get_agent_budget_config()["pause_at_utilization"])
    except Exception:
        return False
    return not blocked


async def _broadcast_status(paused: bool) -> None:
    try:
        from laya.api.websocket import manager
        await manager.broadcast({"type": "agent_budget_status", "paused": paused})
    except Exception as e:
        log.warning("agent_budget_broadcast_failed", error=str(e))
