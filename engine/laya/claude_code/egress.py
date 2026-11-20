# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Sending through the user's Claude Code CLI: a Slack message or reply, a
Gmail draft.

Connecting Slack or Gmail (connect.CONNECT_HOOKS) adds an `egress_connections`
row `claude_code:<platform>` with no n8n credential; disconnecting removes it.
The egress router hands a request to `ClaudeCodeBackend` when it names that
connection, or when it has no connection and its source event was collected
through Claude Code.

A send is one `claude -p` turn whose allowlist holds exactly one tool: the
discovered Slack send tool, or the Gmail `create_draft` tool. Every other
discovered MCP tool is denied and built-in tools are removed (cli.build_args).
Gmail is never given a tool that sends mail: every Gmail action becomes a
draft the user sends from Gmail.

The backend is reached only through the egress execute path (`POST
/egress/execute`, the card executor), which runs on the user's click. Chat
tools only preview, and processing rules skip outbound actions.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from laya.claude_code import cli, connect, discover, store
from laya.claude_code.discover import SEND_ROLES, PlatformDiscovery
from laya.db.sqlite import get_db
from laya.db.timeutil import db_now
from laya.egress.backends.base import EgressBackend
from laya.egress.models import EgressRequest, EgressResult

log = structlog.get_logger()

PREFIX = "claude_code:"

# Action types each platform accepts here. The Slack names are the Slack
# platform contract's; Gmail's contract action `send_email` (a new mail, or a
# reply when it carries a thread) and the draft names all create a draft.
ACTIONS: dict[str, tuple[str, ...]] = {
    "slack": ("reply_thread", "send_message"),
    "gmail": ("create_draft", "reply", "send_email"),
}

# Stored on the connection row; what the connection does.
CAPABILITIES: dict[str, list[str]] = {
    "slack": ["reply_thread", "send_message"],
    "gmail": ["create_draft", "reply"],
}

SEND_TIMEOUT = 300.0

_SLACK_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "permalink": {"type": "string"},
        "ts": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["ok", "permalink", "ts", "error"],
}

_GMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "draft_id": {"type": "string"},
        "thread_id": {"type": "string"},
        "url": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["ok", "draft_id", "thread_id", "url", "error"],
}


def connection_id(platform: str) -> str:
    return f"{PREFIX}{platform}"


def is_claude_code(connection_id_: str | None) -> bool:
    return bool(connection_id_) and connection_id_.startswith(PREFIX)


def send_tool(platform: str, tools: dict[str, str]) -> str | None:
    """The one tool a send turn may use, or None when there is none.

    Gmail only ever gets a draft tool; a discovered name that is not
    `create_draft` is refused rather than allowed.
    """
    role = SEND_ROLES.get(platform)
    tool = tools.get(role) if role else None
    if not tool:
        return None
    if platform == "gmail" and (not tool.endswith("create_draft") or "send" in tool.lower()):
        return None
    return tool


# ── connection status ────────────────────────────────────────────────────


def _status(platform: str, tools: dict[str, str], found: PlatformDiscovery | None,
            all_tools: list[str] | None) -> tuple[str, str | None]:
    label = store.LABELS[platform]
    tool = send_tool(platform, tools)
    if found is not None and not found.available:
        return "error", found.problem or f"{label} is not available in Claude Code"
    if tool is None:
        what = "draft" if platform == "gmail" else "send"
        return "error", f"Claude Code has no {label} {what} tool"
    if all_tools and tool not in all_tools:
        return "error", f"Claude Code no longer offers {tool}"
    return "connected", None


async def connection_status(platform: str, fresh: bool = False) -> tuple[str, str | None]:
    """(status, error) of the platform's send connection.

    Uses the cached discovery unless `fresh`; without any discovery the
    tools stored at connect time decide.
    """
    source = await store.get_source(platform)
    if source is None:
        return "error", f"{store.LABELS[platform]} is not connected through Claude Code"
    found = await discover.discover(force=True, with_identity=False) if fresh else discover.cached()
    pd = found.platforms.get(platform) if found else None
    tools = (pd.tools if pd and pd.tools else None) or source.get("tools") or {}
    return _status(platform, tools, pd, found.all_tools if found else None)


async def _set_status(cid: str, status: str, error: str | None) -> None:
    now = db_now()
    db = await get_db()
    await db.execute(
        """UPDATE egress_connections SET status = ?, error_message = ?,
               last_validated_at = CASE WHEN ? = 'connected' THEN ? ELSE last_validated_at END,
               updated_at = ?
           WHERE connection_id = ?""",
        (status, error, status, now, now, cid),
    )
    await db.commit()


async def test_connection(cid: str) -> tuple[bool, str | None]:
    """`connections.test_connection` for a Claude Code row: fresh discovery."""
    platform = cid[len(PREFIX):]
    if platform not in SEND_ROLES:
        return False, "Connection not found"
    status, error = await connection_status(platform, fresh=True)
    await _set_status(cid, status, error)
    return status == "connected", error


async def remove_connection(cid: str) -> None:
    """`connections.remove_connection` for a Claude Code row.

    Removes only the send connection; collecting stays connected until the
    platform is disconnected in the Claude Code settings.
    """
    db = await get_db()
    await db.execute("DELETE FROM egress_connections WHERE connection_id = ?", (cid,))
    await db.commit()
    log.info("claude_code_send_connection_removed", connection_id=cid)


# ── connect / disconnect hooks ───────────────────────────────────────────


async def on_connect(platform: str, pd: PlatformDiscovery) -> None:
    if platform not in SEND_ROLES:
        return
    status, error = _status(platform, pd.tools, pd, discover.cached().all_tools if discover.cached() else None)
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT space_id FROM sources WHERE workflow_id = ?", (connection_id(platform),)
    )
    space_id = rows[0]["space_id"] if rows else None
    now = db_now()
    await db.execute(
        """INSERT INTO egress_connections
               (connection_id, platform, name, n8n_credential_id, space_id, status,
                capabilities, error_message, last_validated_at, created_at, updated_at)
           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(connection_id) DO UPDATE SET
               name = excluded.name, space_id = excluded.space_id, status = excluded.status,
               capabilities = excluded.capabilities, error_message = excluded.error_message,
               last_validated_at = excluded.last_validated_at, updated_at = excluded.updated_at""",
        (connection_id(platform), platform, f"{store.LABELS[platform]} (Claude Code)", space_id,
         status, json.dumps(CAPABILITIES[platform]), error,
         now if status == "connected" else None, now, now),
    )
    await db.commit()
    log.info("claude_code_send_connection_saved", platform=platform, status=status)


async def on_disconnect(platform: str) -> None:
    if platform in SEND_ROLES:
        await remove_connection(connection_id(platform))


def register_hooks() -> None:
    if on_connect not in connect.CONNECT_HOOKS:
        connect.CONNECT_HOOKS.append(on_connect)
    if on_disconnect not in connect.DISCONNECT_HOOKS:
        connect.DISCONNECT_HOOKS.append(on_disconnect)


# ── routing ──────────────────────────────────────────────────────────────


async def _event_connection(event_id: str) -> str | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT source_connection_id FROM events WHERE event_id = ?", (event_id,)
    )
    return rows[0]["source_connection_id"] if rows else None


async def _row_exists(cid: str) -> bool:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT 1 FROM egress_connections WHERE connection_id = ?", (cid,)
    )
    return bool(rows)


async def backend_for(request: EgressRequest) -> ClaudeCodeBackend | None:
    """The backend for a request that belongs to Claude Code, else None.

    A request naming a `claude_code:` connection always belongs here. A
    request with no connection belongs here when its source event was
    collected through Claude Code, the platform's send connection exists, and
    the action is one this backend performs; anything else (including every
    request naming an n8n connection) stays with the other backends.
    """
    if request.connection_id:
        return BACKEND if is_claude_code(request.connection_id) else None
    if not request.source_event_id:
        return None
    cid = await _event_connection(request.source_event_id)
    if (
        cid == connection_id(request.platform)
        and BACKEND.supports(request.platform, request.action_type)
        and await _row_exists(cid)
    ):
        request.connection_id = cid
        return BACKEND
    return None


# ── payload → prompt ─────────────────────────────────────────────────────


def _addresses(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [a.strip() for a in str(value or "").split(",") if a.strip()]


async def _enriched_payload(request: EgressRequest) -> tuple[dict, dict]:
    """The payload with identifiers from the source event, platform-normalized."""
    from laya.egress.enrichment import enrich_payload_from_event

    probe = EgressRequest(
        platform=request.platform,
        # Gmail's contract names its mail action send_email; normalizing under
        # that name applies the reply subject (`Re: <original>`) for threads.
        action_type="send_email" if request.platform == "gmail" else request.action_type,
        payload=request.payload,
        source_event_id=request.source_event_id,
    )
    payload, event_ctx = await enrich_payload_from_event(probe)
    if request.platform == "slack" and not payload.get("channel"):
        # Events collected through Claude Code carry the channel as
        # `slack_channel_id`; the Slack contract derives `channel` only from
        # `slack_channel`.
        try:
            meta = json.loads(event_ctx.get("content_metadata") or "{}")
        except (TypeError, ValueError):
            meta = {}
        if meta.get("slack_channel_id"):
            payload["channel"] = meta["slack_channel_id"]
    return payload, event_ctx


def slack_arguments(action_type: str, payload: dict) -> tuple[dict | None, list[str]]:
    channel = str(payload.get("channel") or "").lstrip("#")
    text = payload.get("message") or payload.get("text") or ""
    errors = []
    if not channel:
        errors.append("Missing 'channel'")
    if not text:
        errors.append("Missing message text")
    args: dict[str, Any] = {"channel_id": channel, "message": text}
    if action_type == "reply_thread":
        if not payload.get("thread_ts"):
            errors.append("Missing 'thread_ts' for thread reply")
        args["thread_ts"] = str(payload.get("thread_ts") or "")
    return (None if errors else args), errors


def gmail_arguments(payload: dict) -> tuple[dict | None, list[str]]:
    to = _addresses(payload.get("to"))
    body = payload.get("body") or ""
    errors = []
    if not to:
        errors.append("Missing 'to' recipient address")
    if not body:
        errors.append("Missing email 'body'")
    args: dict[str, Any] = {"to": to, "subject": payload.get("subject") or "", "body": body}
    for key in ("cc", "bcc"):
        if _addresses(payload.get(key)):
            args[key] = _addresses(payload.get(key))
    if payload.get("thread_id"):
        args["threadId"] = payload["thread_id"]
    reply_to = payload.get("reply_to_message_id") or payload.get("gmail_id")
    if reply_to:
        args["replyToMessageId"] = reply_to
    return (None if errors else args), errors


def build_prompt(platform: str, tool: str, args: dict) -> str:
    what = "post this Slack message" if platform == "slack" else "create this Gmail DRAFT (never send it)"
    threading = ""
    if platform == "gmail" and "threadId" in args:
        threading = (
            "\nIf the tool has no threadId parameter, leave threadId out; "
            "never call another tool to look anything up."
        )
    result = (
        "ok, permalink (the message link the tool returns) and ts (the message timestamp)"
        if platform == "slack" else
        "ok, draft_id, thread_id and url (from the draft the tool returns)"
    )
    return (
        f"Your only task: {what}. Call the tool {tool} exactly once with exactly these "
        "arguments. Copy every value verbatim: do not rewrite, shorten, translate, "
        "format or add anything, and do not add arguments.\n"
        f"ARGUMENTS (JSON):\n{json.dumps(args, ensure_ascii=False, indent=2)}\n"
        "If a parameter of the tool has a different name for one of these values, use "
        f"its equivalent.{threading}\n"
        "Do not call any other tool, and do not call the tool a second time, even if it fails.\n"
        f"Then answer with {result}; set ok to false and error to the tool's error "
        "message if the call failed, otherwise error is an empty string."
    )


def check_result(platform: str, data: dict) -> tuple[dict | None, str | None]:
    """(structured output, None) when the answer shows a completed call, else (None, error)."""
    denials = data.get("permission_denials") or []
    if denials:
        names = sorted({str(d.get("tool_name", "?")) for d in denials if isinstance(d, dict)})
        return None, f"Claude Code tried tools it is not allowed to use: {', '.join(names) or 'unknown'}"
    out = data.get("structured_output")
    if not isinstance(out, dict):
        return None, "Claude Code returned no result"
    if not out.get("ok"):
        return None, out.get("error") or "the tool call failed"
    if platform == "slack":
        if not (out.get("permalink") or out.get("ts")):
            return None, "Claude Code reported success but returned no message link"
        link = out.get("permalink") or ""
        if link and not link.startswith("https://"):
            return None, f"Claude Code returned an invalid message link: {link[:200]}"
    elif not out.get("draft_id"):
        return None, "Claude Code reported success but returned no draft id"
    return out, None


# ── backend ──────────────────────────────────────────────────────────────


class ClaudeCodeBackend(EgressBackend):
    """Runs one send turn of the user's `claude` CLI per request."""

    def supports(self, platform: str, action_type: str) -> bool:
        return action_type in ACTIONS.get(platform, ())

    async def health_check(self) -> bool:
        return await cli.cli_version() is not None

    async def execute(self, request: EgressRequest, credentials: dict) -> EgressResult:
        platform = request.platform
        label = store.LABELS.get(platform, platform)
        if request.connection_id and request.connection_id != connection_id(platform):
            return EgressResult(success=False, error=(
                f"Connection {request.connection_id} cannot send to {label}"))
        if not self.supports(platform, request.action_type):
            return EgressResult(success=False, error=(
                f"'{request.action_type}' on {label} is not available through Claude Code"))

        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT status, error_message FROM egress_connections WHERE connection_id = ?",
            (connection_id(platform),),
        )
        if not rows:
            return EgressResult(success=False, error=f"{label} sending through Claude Code is not connected")
        if rows[0]["status"] != "connected":
            return EgressResult(success=False, error=(
                f"{label} sending through Claude Code is not available: "
                f"{rows[0]['error_message'] or rows[0]['status']}"))

        from laya.claude_code import accounts

        source = await store.get_source(platform)
        # The send runs on a Claude account that has the platform and its send
        # tool, with that account's own tool names; it is never failed over.
        assignment = await accounts.pick_for_platform(platform, (SEND_ROLES[platform],))
        if assignment is None:
            return EgressResult(success=False, error=(
                f"No Claude account has {label} with its send tool connected. "
                "Check the connectors in Settings → Claude accounts."))
        tool = send_tool(platform, assignment.tools)
        if tool is None:
            return EgressResult(success=False, error=f"Claude Code has no {label} send tool")

        try:
            payload, _ = await _enriched_payload(request)
        except Exception as e:
            return EgressResult(success=False, error=f"Could not prepare the {label} payload: {e}")
        if platform == "slack":
            args, errors = slack_arguments(request.action_type, payload)
        else:
            args, errors = gmail_arguments(payload)
        if errors:
            return EgressResult(success=False, error=f"Invalid {label} payload: " + "; ".join(errors))

        model = store.merged_config(platform, (source or {}).get("config")).get("model") or "haiku"
        deny = sorted(set(assignment.all_tools) - {tool})
        try:
            data = await cli.run_send(
                platform, build_prompt(platform, tool, args), tool,
                model=model, timeout=SEND_TIMEOUT,
                schema=_SLACK_SCHEMA if platform == "slack" else _GMAIL_SCHEMA, deny=deny,
                env=assignment.env or None, account_id=assignment.account_id,
            )
        except cli.ClaudeCodeError as e:
            # Not retryable: the tool may have run before the turn failed.
            return EgressResult(success=False, error=f"Claude Code could not complete the {label} action: {e}")

        out, error = check_result(platform, data)
        if error:
            return EgressResult(success=False, error=error)
        if platform == "slack":
            return EgressResult(
                success=True, result_url=out.get("permalink") or None,
                result_data={"via": "claude_code", "permalink": out.get("permalink"), "ts": out.get("ts")},
            )
        return EgressResult(
            success=True, result_url=out.get("url") or None,
            result_data={"via": "claude_code", "draft": True, "sent": False,
                         "draft_id": out["draft_id"], "thread_id": out.get("thread_id")},
        )


BACKEND = ClaudeCodeBackend()

register_hooks()
