# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""REST endpoints of the Claude Code connector (`/claude-code/...`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from laya.claude_code import connect as connect_mod
from laya.claude_code import egress as _egress  # noqa: F401  registers the send connection hooks
from laya.claude_code import discover as discover_mod
from laya.claude_code import my_work, service, store, work_actions
from laya.claude_code.discover import SEND_ROLES, Discovery, PlatformDiscovery

router = APIRouter(prefix="/claude-code", tags=["claude-code"])


def _check_platform(platform: str) -> None:
    if platform not in store.PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown platform '{platform}'")


def platform_status(platform: str, found: Discovery | None, source: dict[str, Any] | None) -> dict[str, Any]:
    pd = (found.platforms.get(platform) if found else None) or PlatformDiscovery(
        problem="Discovery has not run yet."
    )
    tools = (source or {}).get("tools") or pd.tools
    return {
        "available": pd.available,
        "connected": source is not None,
        "enabled": bool(source and source["enabled"]),
        "identity": (source or {}).get("identity") or pd.identity,
        "problem": pd.problem,
        "last_run_at": (source or {}).get("last_run_at"),
        "last_ok_at": (source or {}).get("last_ok_at"),
        "last_error": (source or {}).get("last_error"),
        "last_count": (source or {}).get("last_count"),
        "config": (source or {}).get("config") or store.default_config(platform),
        "can_send": platform in SEND_ROLES and bool(tools.get(SEND_ROLES[platform])),
    }


async def build_status(found: Discovery | None) -> dict[str, Any]:
    from laya.pipeline.agent_budget import is_agent_budget_paused

    sources = {s["platform"]: s for s in await store.list_sources()}
    return {
        "cli": (found.cli if found else None) or {"found": False, "path": None, "version": None},
        "platforms": {p: platform_status(p, found, sources.get(p)) for p in store.PLATFORMS},
        "budget_paused": await is_agent_budget_paused(),
    }


@router.get("/status")
async def get_status() -> dict[str, Any]:
    # Listing tools costs no tokens; identities are read on discover/connect.
    found = discover_mod.cached() or await discover_mod.discover(with_identity=False)
    return await build_status(found)


@router.get("/my-work")
async def get_my_work(refresh: bool = False) -> dict[str, Any]:
    """The user's open Linear issues and own open GitHub pull requests."""
    return await my_work.get_my_work(refresh=refresh)


def _action_error(e: work_actions.ActionError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


@router.get("/my-work/linear/states")
async def get_linear_states(team: str, refresh: bool = False) -> dict[str, Any]:
    """A Linear team's workflow states, for the state menu of My work."""
    try:
        return {"team": team.upper(), "states": await work_actions.team_states(team, refresh=refresh)}
    except work_actions.ActionError as e:
        raise _action_error(e) from e


@router.post("/my-work/linear/{identifier}/action")
async def post_linear_action(identifier: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """One Linear action on the user's click: `{action: state|assign_me|priority|comment,
    value, dry_run?}`. Returns the issue as Linear reports it afterwards."""
    try:
        return await work_actions.linear_action(
            identifier, str(body.get("action") or ""), body.get("value"),
            dry_run=bool(body.get("dry_run")),
        )
    except work_actions.ActionError as e:
        raise _action_error(e) from e


@router.post("/my-work/github/action")
async def post_github_action(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """One action on the user's own pull request: `{repo, number, action:
    ready|draft|reviewers|comment|update_branch|merge, reviewers?, body?, method?,
    head_sha?, delete_branch?, dry_run?}`. Returns the PR as GitHub reports it afterwards."""
    try:
        return await work_actions.github_action(
            str(body.get("repo") or ""), body.get("number"), str(body.get("action") or ""),
            {k: body.get(k) for k in ("reviewers", "body", "method", "head_sha", "delete_branch")},
            dry_run=bool(body.get("dry_run")),
        )
    except work_actions.ActionError as e:
        raise _action_error(e) from e


@router.post("/discover")
async def run_discover() -> dict[str, Any]:
    found = await discover_mod.discover(force=True, with_identity=True)
    await discover_mod.refresh_stored_tools(found)
    return await build_status(found)


@router.post("/{platform}/connect")
async def connect_platform(platform: str, body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    _check_platform(platform)
    try:
        await connect_mod.connect(platform, (body or {}).get("space_id"))
    except connect_mod.ConnectError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return platform_status(platform, discover_mod.cached(), await store.get_source(platform))


@router.delete("/{platform}")
async def disconnect_platform(platform: str) -> dict[str, Any]:
    _check_platform(platform)
    await connect_mod.disconnect(platform)
    return {"status": "disconnected", "platform": platform}


@router.put("/{platform}/config")
async def put_config(platform: str, config: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _check_platform(platform)
    try:
        await connect_mod.configure(platform, config)
    except connect_mod.ConnectError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return platform_status(platform, discover_mod.cached(), await store.get_source(platform))


@router.put("/{platform}/enabled")
async def put_enabled(platform: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Pause or resume a connected platform's sweeps without disconnecting it."""
    _check_platform(platform)
    if await store.get_source(platform) is None:
        raise HTTPException(status_code=404, detail=f"{store.LABELS[platform]} is not connected")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' must be true or false")
    await store.upsert_source(platform, enabled=enabled)
    return platform_status(platform, discover_mod.cached(), await store.get_source(platform))


@router.post("/{platform}/sync")
async def sync_platform(platform: str) -> dict[str, Any]:
    _check_platform(platform)
    if await store.get_source(platform) is None:
        raise HTTPException(status_code=404, detail=f"{store.LABELS[platform]} is not connected")
    return await service.sweep(platform)
