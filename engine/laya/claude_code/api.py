# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""REST endpoints of the Claude Code connector (`/claude-code/...`)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

from laya.claude_code import connect as connect_mod
from laya.claude_code import egress as _egress  # noqa: F401  registers the send connection hooks
from laya.claude_code import discover as discover_mod
from laya.claude_code import accounts, cli, codex_accounts, my_work, service, store, terminal, work_actions
from laya.claude_code.discover import SEND_ROLES, Discovery, PlatformDiscovery
from laya.security.engine_auth import WS_SUBPROTOCOL, offered_subprotocols

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
    """The user's open Linear issues and own open GitHub pull requests, as
    stored; returns at once. A stale section, or every section with
    `refresh=true`, is refreshed in the background (`refreshing` per section;
    a `my_work_updated` websocket message when a refresh has stored its result)."""
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


# ── Claude accounts (claude_code/accounts.py) ─────────────────────────────


def _account_error(e: accounts.AccountError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


@router.get("/accounts")
async def get_accounts(refresh: bool = False) -> dict[str, Any]:
    """The Claude accounts, their usage, connectors and the running sign-in."""
    return await accounts.overview(refresh=refresh)


@router.get("/github/orgs")
async def github_orgs() -> dict[str, Any]:
    """The GitHub organizations the `gh` login belongs to (``gh api user/orgs``) and the
    GitHub source's configured scope (``orgs``; empty = every repository)."""
    try:
        data = await cli.run_gh("api", "user/orgs", "--paginate")
        orgs = sorted({o["login"] for o in data or [] if isinstance(o, dict) and o.get("login")}, key=str.lower)
        error = None
    except cli.ClaudeCodeError as e:
        orgs, error = [], str(e)
    source = await store.get_source("github")
    selected = list(((source or {}).get("config") or {}).get("orgs") or [])
    return {"orgs": orgs, "selected": selected, "connected": source is not None, "error": error}


@router.get("/gmail/senders")
async def gmail_senders(days: int = 30, limit: int = 20) -> dict[str, Any]:
    """Senders seen in recent Gmail events and not skipped yet (suggestions for the
    "Skip mail from" list). Read-only."""
    from laya.claude_code import pickers

    return await pickers.gmail_senders(days=days, limit=limit)


@router.get("/folders/check")
async def check_folder(path: str = "") -> dict[str, Any]:
    """Whether ``path`` is an existing folder (the browser build's checkout folder input)."""
    from laya.claude_code import pickers

    return pickers.check_folder(path)


@router.get("/assistant/rules")
async def assistant_rules() -> dict[str, Any]:
    """The assistant's working rules, the neutral default, and whether the user wrote
    their own (set with ``PUT /settings {"assistant": {"rules": "…"}}``; empty = default)."""
    from laya.claude_code import assistant
    from laya.config import load_settings

    custom = (load_settings().get("assistant") or {}).get("rules")
    return {"rules": assistant.rules(), "default": assistant.DEFAULT_RULES,
            "custom": bool(isinstance(custom, str) and custom.strip())}


@router.get("/auto")
async def auto_routing() -> dict[str, Any]:
    """What each role set to Automatic (``agent/auto``) runs on right now."""
    from laya.config import load_settings
    from laya.llm import agent_backend as ab

    settings = load_settings()
    models = settings.get("models") or {}
    picks = {
        False: await ab.choose_auto_agent(streaming=False, commit=False),
        True: await ab.choose_auto_agent(streaming=True, commit=False),
    }
    labels = accounts.LOGIN_AGENTS

    async def login_label(agent_id: str, login: str | None) -> str | None:
        if login is None:
            return None
        if agent_id == codex_accounts.AGENT_ID:
            return await codex_accounts.label(login)
        return await accounts.account_label(login)

    roles: dict[str, Any] = {}
    for role in ("router", "stager", "omni", "chat", "trace"):
        if models.get(role) != ab.AUTO_MODEL:
            roles[role] = {"automatic": False, "model": models.get(role)}
            continue
        streaming = role in ("chat", "trace")
        agent_id = picks[streaming]
        if agent_id is not None:
            login, _ = await ab._agent_state(agent_id)
            roles[role] = {
                "automatic": True, "via": "agent", "agent_id": agent_id, "agent_label": labels.get(agent_id, agent_id),
                "login": login, "login_label": await login_label(agent_id, login),
                "model": ab.auto_model_for(agent_id, role),
            }
            continue
        fallback = ab.api_fallback_model(role)
        roles[role] = {
            "automatic": True, "via": "api" if fallback else "none", "model": fallback,
            "reason": None if fallback else (
                "No streaming agent login (Claude Code) with room." if streaming
                else "No coding agent login with room."),
        }
    return {
        "roles": roles,
        "api_fallback": bool((settings.get("model_access") or {}).get("api_fallback")),
    }


@router.get("/accounts/login")
async def current_login() -> dict[str, Any]:
    """The latest sign-in (account or connector, running or ended), or null."""
    return {"login": accounts.login_state()}


@router.post("/accounts")
async def add_account(body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
    """Start signing in a new login of ``agent`` (default claude_code); returns the
    sign-in (poll its status)."""
    agent = str((body or {}).get("agent") or accounts.AGENT_ID)
    try:
        if agent == codex_accounts.AGENT_ID:
            return await codex_accounts.start_login()
        if agent != accounts.AGENT_ID:
            raise accounts.AccountError(f"Logins of '{agent}' cannot be added")
        return await accounts.start_login()
    except accounts.AccountError as e:
        raise _account_error(e) from e


@router.post("/accounts/{account_id}/login")
async def login_account(account_id: str) -> dict[str, Any]:
    try:
        if await accounts.agent_of(account_id) == codex_accounts.AGENT_ID:
            return await codex_accounts.start_login(account_id)
        return await accounts.start_login(account_id)
    except accounts.AccountError as e:
        raise _account_error(e) from e


@router.post("/accounts/{account_id}/code")
async def submit_account_code(account_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """The code from the sign-in page; it is passed to the CLI and never stored."""
    try:
        return await accounts.submit_code(account_id, str(body.get("code") or ""))
    except accounts.AccountError as e:
        raise _account_error(e) from e


@router.get("/accounts/{account_id}/status")
async def account_login_status(account_id: str) -> dict[str, Any]:
    return {"login": accounts.flow_status(account_id)}


@router.post("/accounts/{account_id}/cancel")
async def cancel_account_login(account_id: str) -> dict[str, Any]:
    try:
        return await accounts.cancel(account_id)
    except accounts.AccountError as e:
        raise _account_error(e) from e


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str) -> dict[str, Any]:
    try:
        if await accounts.agent_of(account_id) == codex_accounts.AGENT_ID:
            await codex_accounts.remove(account_id)
        else:
            await accounts.remove(account_id)
    except accounts.AccountError as e:
        raise _account_error(e) from e
    return {"status": "removed", "id": account_id}


@router.post("/accounts/{account_id}/connectors/check")
async def check_account_connectors(account_id: str) -> dict[str, Any]:
    try:
        if await accounts.agent_of(account_id) == codex_accounts.AGENT_ID:
            return await codex_accounts.check_connectors(account_id)
        return await accounts.check_connectors(account_id)
    except (accounts.AccountError, codex_accounts.AppServerError) as e:
        if isinstance(e, codex_accounts.AppServerError):
            raise HTTPException(status_code=502, detail=str(e)) from e
        raise _account_error(e) from e


@router.post("/accounts/{account_id}/connectors/{connector}/add")
async def add_account_connector(account_id: str, connector: str) -> dict[str, Any]:
    try:
        return await accounts.add_connector(account_id, connector)
    except accounts.AccountError as e:
        raise _account_error(e) from e


@router.post("/accounts/{account_id}/connectors/{connector}/login")
async def login_account_connector(account_id: str, connector: str) -> dict[str, Any]:
    try:
        return await accounts.start_connector_login(account_id, connector)
    except accounts.AccountError as e:
        raise _account_error(e) from e


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


# ── Claude terminal (terminal.py) ────────────────────────────────────────
# These paths have two or more segments after `/terminal`, so they never
# collide with the `/{platform}` and `/{platform}/<action>` routes above.


def _terminal_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except terminal.TerminalError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


async def _terminal_acall(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except terminal.TerminalError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


def _int_field(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail=f"'{key}' must be an integer")
    return value


@router.get("/terminal/sessions")
async def list_terminal_sessions() -> dict[str, Any]:
    """Open terminal sessions, the closed ones that can be resumed, and the default cwd."""
    return {
        "sessions": terminal.list_open(),
        "closed": await terminal.list_closed(),
        "default_cwd": terminal.default_cwd(),
        "agents": terminal.list_agents(),
        "default_agent": terminal.default_agent(),
    }


@router.post("/terminal/sessions")
async def create_terminal_session(body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """Start a coding agent in a new terminal session (`{cwd?, name?, resume?, cols?, rows?, agent?}`).

    `resume` is the id of a closed session; its Claude conversation continues
    with `claude --resume`.
    """
    body = body or {}
    for key in ("cwd", "name", "resume", "agent"):
        if body.get(key) is not None and not isinstance(body[key], str):
            raise HTTPException(status_code=422, detail=f"'{key}' must be a string")
    return await _terminal_acall(
        terminal.create,
        cwd=body.get("cwd"),
        name=body.get("name"),
        resume=body.get("resume"),
        cols=_int_field(body, "cols"),
        rows=_int_field(body, "rows"),
        agent=body.get("agent"),
    )


@router.patch("/terminal/sessions/{session_id}")
async def rename_terminal_session(session_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = body.get("name")
    if not isinstance(name, str):
        raise HTTPException(status_code=422, detail="'name' must be a string")
    return await _terminal_acall(terminal.rename, session_id, name)


@router.delete("/terminal/sessions/{session_id}")
async def close_terminal_session(session_id: str) -> dict[str, Any]:
    await _terminal_acall(terminal.close, session_id)
    return {"status": "closed", "id": session_id}


@router.post("/terminal/{session_id}/resize")
async def resize_terminal(session_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    cols, rows = _int_field(body, "cols"), _int_field(body, "rows")
    if cols is None or rows is None:
        raise HTTPException(status_code=422, detail="'cols' and 'rows' are required")
    return _terminal_call(terminal.resize, session_id, cols, rows)


@router.post("/terminal/{session_id}/input")
async def terminal_input(session_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Insert text at the session's prompt as a bracketed paste; Enter is never sent."""
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=422, detail="'text' must be a string")
    return await _terminal_acall(terminal.paste, session_id, text)


@router.websocket("/terminal/{session_id}/pty")
async def terminal_pty(websocket: WebSocket, session_id: str) -> None:
    """Bidirectional PTY stream of one terminal session.

    The handshake is authenticated like `/ws` (the router's auth dependency
    applies `require_ws_engine_auth` to websocket routes; the origin guard runs
    before routing). Server → client: binary frames of terminal output (the
    buffered recent output first), and text frames `{"type": "status", ...}` /
    `{"type": "exit", "code": n}`. Client → server: binary frames of input
    bytes, and text frames `{"type": "resize", "cols", "rows"}` or
    `{"type": "input", "data": "<text>"}`.
    """
    offered = offered_subprotocols(websocket)
    await websocket.accept(subprotocol=WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None)
    try:
        session = terminal.get(session_id)
    except terminal.TerminalError:
        await websocket.close(code=4404, reason="Terminal session not found")
        return

    # Subscribing and copying the buffer happen without an await in between, so
    # no output chunk is lost or sent twice.
    queue = terminal.subscribe(session)
    backlog = bytes(session.buffer)

    async def pump() -> None:
        if backlog:
            await websocket.send_bytes(backlog)
        await websocket.send_text(json.dumps({"type": "status", **session.to_dict()}))
        if not session.running and session.exit_code is not None:
            await websocket.send_text(json.dumps({"type": "exit", "code": session.exit_code}))
        while True:
            item = await queue.get()
            if item is None:
                await websocket.close(code=1000, reason="Terminal session closed")
                return
            if isinstance(item, bytes):
                await websocket.send_bytes(item)
            else:
                await websocket.send_text(json.dumps(item))

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                terminal.write(session, message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "resize":
                cols, rows = data.get("cols"), data.get("rows")
                if isinstance(cols, int) and isinstance(rows, int):
                    try:
                        terminal.resize(session_id, cols, rows)
                    except terminal.TerminalError:
                        pass
            elif data.get("type") == "input" and isinstance(data.get("data"), str):
                terminal.write(session, data["data"].encode("utf-8"))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        terminal.unsubscribe(session, queue)
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
