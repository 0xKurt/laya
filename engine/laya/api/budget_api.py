# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Budget REST API — monthly cost limits and workflow pause/resume."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from laya.pipeline.budget import (
    check_budget,
    get_budget_config,
    get_current_month_cost,
    get_monthly_history,
    get_paused_workflow_count,
    is_budget_paused,
    resume_from_budget,
    update_budget_config,
)

log = structlog.get_logger()
router = APIRouter()


@router.get("/budget")
async def get_budget() -> dict:
    """Get budget configuration, current month cost, and pause status."""
    config = await get_budget_config()
    cost = await get_current_month_cost()
    paused = await is_budget_paused()
    paused_count = await get_paused_workflow_count() if paused else 0

    return {
        "monthly_limit_usd": config["monthly_limit_usd"],
        "enabled": config["enabled"],
        "current_month_cost": cost["total_cost_usd"],
        "current_month": cost["year_month"],
        "by_model": cost["by_model"],
        "tokens_by_model": cost["tokens_by_model"],
        "by_feature": cost["by_feature"],
        "by_step": cost["by_step"],
        "total_input_tokens": cost["total_input_tokens"],
        "total_output_tokens": cost["total_output_tokens"],
        "is_paused": paused,
        "paused_workflow_count": paused_count,
    }


@router.put("/budget")
async def set_budget(body: dict) -> dict:
    """Update budget configuration.

    Body: {"monthly_limit_usd": 10.0, "enabled": true}
    Setting monthly_limit_usd to null disables the limit.
    """
    limit = body.get("monthly_limit_usd")
    enabled = body.get("enabled", False)

    config = await update_budget_config(limit, enabled)
    log.info("budget_config_updated", limit=limit, enabled=enabled)

    # If budget was just enabled, run an immediate check
    if enabled and limit is not None:
        await check_budget()

    return {
        "status": "ok",
        "monthly_limit_usd": config["monthly_limit_usd"],
        "enabled": config["enabled"],
    }


@router.get("/budget/history")
async def get_budget_history(months: int = 12) -> dict:
    """Get monthly cost history."""
    history = await get_monthly_history(limit=months)
    return {"months": history}


@router.post("/budget/resume")
async def manual_resume() -> dict:
    """Manually resume workflows that were paused by budget enforcement."""
    try:
        result = await resume_from_budget()
    except Exception as e:
        log.error("budget_resume_failed", error=str(e), error_type=type(e).__name__)
        raise
    log.info("budget_manual_resume", **result)
    return {
        "status": "resumed",
        "resumed_count": result["resumed_count"],
        "errors": result["errors"],
    }


# ---------------------------------------------------------------------------
# Agent inference backend — usage-limit budgeting (window-based, auto-resume)
# ---------------------------------------------------------------------------


@router.get("/agent-budget")
async def get_agent_budget() -> dict:
    """Per-agent window usage, limits, native rate-limit signal, and pause state."""
    from laya.pipeline.agent_budget import get_agent_budget_status

    return await get_agent_budget_status()


@router.put("/agent-budget")
async def set_agent_budget(body: dict) -> dict:
    """Update agent-usage budget config.

    Body: {"enabled": true, "agents": {"claude_code": {"window_token_limit": 5000000,
           "window_hours": 5, "pause_at_percent": 85}},
           "daily_call_limit": 400, "pause_at_utilization": {"five_hour": 0.9, "seven_day": 0.8}}
    daily_call_limit (0 = off) and pause_at_utilization (fractions 0-1 per native window
    type) are optional; when left out they keep their stored values.
    """
    from laya.pipeline.agent_budget import evaluate_agent_budget, update_agent_budget_config

    enabled = bool(body.get("enabled", False))
    agents = body.get("agents")
    daily_call_limit = body.get("daily_call_limit")
    if daily_call_limit is not None and (
        isinstance(daily_call_limit, bool) or not isinstance(daily_call_limit, int) or daily_call_limit < 0
    ):
        raise HTTPException(status_code=422, detail="daily_call_limit must be a whole number >= 0")
    thresholds = body.get("pause_at_utilization")
    if thresholds is not None and not (
        isinstance(thresholds, dict)
        and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
            for v in thresholds.values()
        )
    ):
        raise HTTPException(
            status_code=422, detail="pause_at_utilization values must be fractions between 0 and 1"
        )
    config = update_agent_budget_config(enabled, agents, daily_call_limit, thresholds)
    log.info(
        "agent_budget_config_updated", enabled=enabled, agents=list(config["agents"].keys()),
        daily_call_limit=config["daily_call_limit"],
        pause_at_utilization=config["pause_at_utilization"],
    )

    # Run an immediate check so a just-lowered limit takes effect right away.
    if enabled:
        await evaluate_agent_budget()

    return {"status": "ok", **config}


@router.post("/agent-budget/resume")
async def manual_resume_agent_budget() -> dict:
    """Manually resume workflows paused by an agent usage-limit."""
    from laya.pipeline.agent_budget import resume_from_agent_budget

    result = await resume_from_agent_budget()
    log.info("agent_budget_manual_resume", **result)
    return {"status": "resumed", **result}
