# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Background sweeps of the connected Claude Code platforms.

Every minute the loop sweeps each connected, enabled platform that is due:

- due: no cursor yet, or the cursor (start of the last successful sweep,
  stored in the database, so a restart does not trigger a fresh sweep of
  every platform) is at least `interval_minutes` old;
- inside the platform's `active_hours` (in its `timezone`), except the first
  sweep after connecting, which runs at once so a new source shows its
  cards right away;
- the agent budget is not paused (pipeline/agent_budget.py);
- the space of the platform's `sources` row is not paused.

A sweep of an MCP platform runs on one Claude account
(`accounts.pick_for_platform`): an account whose connector check shows the
platform connected and whose own tool listing has its required tools. With
no such account the sweep is recorded as failed and the cursor stays where
it is, so nothing is skipped. The account's environment, tool names and deny
list travel on `source`, so the collector, the re-check and the linked
documents all run on it. GitHub uses `gh` and no account.

A sweep collects the events since the cursor (minus a small overlap; event
ids are stable, so duplicates are dropped), runs the optional enrich step,
posts them through the same code path as `POST /events`, then re-checks old
open cards (recheck.py).

The loop also polls each Claude account's usage (`accounts.poll_due_usage`).

A second loop keeps "My work" fresh (`my_work.run_due_refreshes`): every
minute it starts the background refresh of each connected section whose
last refresh is older than its interval (GitHub 10 min, Linear 30 min),
inside the source's active hours. It is separate from the sweep loop, so a
long sweep never delays it, and it never waits for a refresh.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from laya.claude_code import links  # noqa: F401  (registers the linked-documents enricher)
from laya.claude_code import store
from laya.claude_code.platforms import COLLECTORS
from laya.claude_code.platforms.common import enrich, local_tz, parse_ts

log = structlog.get_logger()

LOOP_SECONDS = 60
MIN_INTERVAL_MINUTES = 5
# Overlap of consecutive sweep windows; duplicates are dropped by event id.
OVERLAP = timedelta(minutes=5)

_task: asyncio.Task | None = None
_my_work_task: asyncio.Task | None = None
_sweep_lock = asyncio.Lock()


def source_id(platform: str) -> str:
    return f"src_claude_code_{platform}"


def within_active_hours(config: dict[str, Any], now: datetime) -> bool:
    hours = config.get("active_hours") or {}
    local = now.astimezone(local_tz(config))
    if local.weekday() not in (hours.get("days") or []):
        return False
    start, end = hours.get("start") or "00:00", hours.get("end") or "24:00"
    return start <= local.strftime("%H:%M") < end


def is_due(source: dict[str, Any], now: datetime) -> bool:
    if not source.get("cursor"):
        return True
    interval = timedelta(minutes=max(MIN_INTERVAL_MINUTES, int(source["config"].get("interval_minutes", 60))))
    try:
        cursor = parse_ts(source["cursor"])
    except ValueError:
        return True
    return now - cursor >= interval


async def space_paused(platform: str) -> bool:
    from laya.db.sqlite import get_db

    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT s.paused FROM sources src JOIN spaces s ON s.space_id = src.space_id
           WHERE src.source_id = ?""",
        (source_id(platform),),
    )
    return bool(rows and rows[0]["paused"])


async def blocked_reason(platform: str) -> str | None:
    """Why a sweep must not run now, or None."""
    from laya.pipeline.agent_budget import is_agent_budget_paused

    if await is_agent_budget_paused():
        return "agent budget paused"
    if await space_paused(platform):
        return "space paused"
    return None


async def _with_deny_list(source: dict[str, Any]) -> dict[str, Any]:
    """Attach every discovered MCP tool, which the collector turn denies
    except for its own read tools."""
    from laya.claude_code import discover

    found = discover.cached()
    if found is None or not found.all_tools:
        try:
            found = await discover.discover(with_identity=False)
            # A fresh listing also refreshes the stored tool names, so a sweep
            # after a restart uses the connector's current names.
            if await discover.refresh_stored_tools(found):
                source = await store.get_source(source["platform"]) or source
        except Exception as e:  # the sweep can still run with the allowlist alone
            log.warning("claude_code_discovery_for_deny_failed", error=str(e))
            found = None
    return {**source, "deny_tools": list(found.all_tools) if found else []}


NO_ACCOUNT_ERROR = "no eligible account"


async def _assign_account(platform: str) -> dict[str, Any] | None:
    """The source fields of the Claude account the platform's sweep runs on, or None."""
    from laya.claude_code import accounts

    try:
        assignment = await accounts.pick_for_platform(platform)
    except Exception as e:
        log.warning("claude_code_account_pick_failed", platform=platform, error=str(e))
        return None
    return assignment.source_fields() if assignment else None


async def sweep(platform: str) -> dict[str, Any]:
    """Collect one platform once and post its events. Returns {events, error}."""
    from laya.api.events import receive_event
    from laya.claude_code.recheck import recheck

    async with _sweep_lock:
        source = await store.get_source(platform)
        if source is None:
            return {"events": 0, "error": "not connected"}
        reason = await blocked_reason(platform)
        if reason:
            return {"events": 0, "error": reason}

        account_fields: dict[str, Any] | None = None
        if platform != "github":
            account_fields = await _assign_account(platform)
            if account_fields is None:
                # The cursor stays: a later sweep on an eligible account
                # collects this window.
                await store.record_run(
                    platform, started_at=datetime.now(timezone.utc).isoformat(), ok=False,
                    error=NO_ACCOUNT_ERROR,
                )
                log.warning("claude_code_sweep_no_account", platform=platform)
                return {"events": 0, "error": NO_ACCOUNT_ERROR}

        started = datetime.now(timezone.utc)
        config = source["config"]
        if source.get("cursor"):
            since = parse_ts(source["cursor"]) - OVERLAP
        else:
            since = started - timedelta(hours=float(config.get("initial_lookback_hours", 12)))
        if account_fields is not None:
            source = {**source, **account_fields}
        else:
            source = await _with_deny_list(source)
        try:
            events = await COLLECTORS[platform](source, since)
            events = await enrich(events, source)
            try:
                events += await recheck(platform, source)
            except Exception as e:  # a failed re-check must not cost the sweep its events
                log.warning("claude_code_recheck_failed", platform=platform, error=str(e))
            for event in events:
                await receive_event(event)
        except Exception as e:
            error = str(e)[:500] or type(e).__name__
            await store.record_run(platform, started_at=started.isoformat(), ok=False, error=error)
            log.warning("claude_code_sweep_failed", platform=platform, error=error)
            return {"events": 0, "error": error}

        await store.record_run(platform, started_at=started.isoformat(), ok=True, count=len(events))
        log.info("claude_code_sweep_done", platform=platform, events=len(events))
        return {"events": len(events), "error": None}


async def run_due_sweeps(now: datetime | None = None) -> list[str]:
    """Sweep every platform that is due now; returns the platforms swept."""
    now = now or datetime.now(timezone.utc)
    swept = []
    for source in await store.list_sources():
        platform = source["platform"]
        if platform not in COLLECTORS or not source["enabled"]:
            continue
        if not is_due(source, now):
            continue
        if source.get("cursor") and not within_active_hours(source["config"], now):
            continue
        if await blocked_reason(platform):
            continue
        await sweep(platform)
        swept.append(platform)
    return swept


async def _loop() -> None:
    from laya.claude_code import accounts

    while True:
        try:
            await run_due_sweeps()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("claude_code_loop_error", error=str(e))
        try:
            await accounts.poll_due_usage()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("claude_code_usage_poll_error", error=str(e))
        await asyncio.sleep(LOOP_SECONDS)


async def _my_work_loop() -> None:
    from laya.claude_code import my_work

    while True:
        try:
            await my_work.run_due_refreshes()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("claude_code_my_work_loop_error", error=str(e))
        await asyncio.sleep(LOOP_SECONDS)


async def start_service() -> None:
    global _task, _my_work_task
    await store.ensure_tables()
    from laya.claude_code import terminal

    await terminal.startup()
    if await store.list_sources():
        # Also on start, so an install connected before this default existed
        # (or set up without an API key) gets subscription models without edits.
        from laya.claude_code.connect import use_subscription_models

        use_subscription_models()
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="claude_code_service")
        log.info("claude_code_service_started")
    if _my_work_task is None or _my_work_task.done():
        _my_work_task = asyncio.create_task(_my_work_loop(), name="claude_code_my_work")


async def stop_service() -> None:
    global _task, _my_work_task
    from laya.claude_code import accounts, my_work, terminal

    await terminal.shutdown_all()
    await accounts.shutdown()
    if _my_work_task is not None:
        _my_work_task.cancel()
        try:
            await _my_work_task
        except (asyncio.CancelledError, Exception):
            pass
        _my_work_task = None
    my_work.reset()
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
        log.info("claude_code_service_stopped")
