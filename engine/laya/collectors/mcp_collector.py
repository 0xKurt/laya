# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""MCP collector — ingest Slack, Gmail, Linear and GitHub without n8n or OAuth apps.

Reads the user's sources through the MCP connectors their Claude Code login already
has (claude.ai Slack/Gmail, the Linear MCP server) and through the `gh` CLI for GitHub,
normalizes what it finds into Laya Events, and hands them to the same path as
`POST /events`. One sweep per source and interval instead of one webhook per message.

Safety: every `claude -p` run gets an explicit allowlist of READ tools only
(`--allowedTools`), and in headless print mode any tool outside that list is denied.
The collector can therefore never send, post, label or modify anything.

Event ids are derived from the source's own message ids (uuid5), so overlapping sweep
windows re-deliver the same ids and the events table drops the duplicates.

The user's own messages are collected too and flagged `content.metadata.is_self`, so
the pipeline can close cards the user already answered.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from laya.config import get_agent_binary, load_settings
from laya.db.sqlite import get_db
from laya.models.event import LayaEvent
from laya.tz import safe_zoneinfo

log = structlog.get_logger()

_collector_task: asyncio.Task | None = None
_sweep_lock = asyncio.Lock()
_last_sweep: dict[str, datetime] = {}
# source -> {"at", "ok", "events", "error", "duration_ms"} for GET /collector/status
_status: dict[str, dict[str, Any]] = {}

SOURCES = ("slack", "gmail", "linear", "github", "notion")

DEFAULT_COLLECTOR_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 60,
    # Sweeps run only inside these hours (local time) on these weekdays (0 = Monday).
    "active_hours": {"start": "08:00", "end": "19:00", "days": [0, 1, 2, 3, 4]},
    "timezone": "Europe/Berlin",
    "model": "haiku",
    "timeout_seconds": 600,
    "initial_lookback_hours": 12,
    "sources": {"slack": True, "gmail": True, "linear": True, "github": True, "notion": True},
    "identity": {
        "slack_user_id": "",
        "github_login": "",
        "emails": [],
    },
    "github_orgs": [],
    # Senders whose mails duplicate another source or are pure notifications.
    "gmail_exclude_senders": [
        "notifications@linear.app",
        "notifications@github.com",
        "noreply@github.com",
        "notify@mail.notion.com",
    ],
}

# Read-only tools per source. Nothing here can send, post, label or change data.
ALLOWED_TOOLS: dict[str, list[str]] = {
    "slack": [
        "mcp__claude_ai_Slack__slack_search_public_and_private",
        "mcp__claude_ai_Slack__slack_read_thread",
        "mcp__claude_ai_Slack__slack_read_channel",
        "mcp__claude_ai_Slack__slack_read_user_profile",
    ],
    "gmail": [
        "mcp__claude_ai_Gmail__search_threads",
        "mcp__claude_ai_Gmail__get_thread",
        "mcp__claude_ai_Gmail__get_message",
        # Gemini notes link a Google Doc; reading it gives decisions and owners.
        "mcp__claude_ai_Google_Drive__read_file_content",
        "mcp__claude_ai_Google_Drive__get_file_metadata",
    ],
    "notion": [
        "mcp__notion__notion-search",
        "mcp__notion__notion-fetch",
        "mcp__notion__notion-get-comments",
        "mcp__notion__notion-get-self",
    ],
    "linear": [
        "mcp__linear-server__get_notifications",
        "mcp__linear-server__list_issues",
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_comments",
    ],
}

_DENIED_BUILTINS = (
    "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task,Agent,TodoWrite"
)


def get_collector_settings() -> dict[str, Any]:
    """Collector settings from settings.json merged over the defaults."""
    stored = load_settings().get("mcp_collector", {}) or {}
    merged = {**DEFAULT_COLLECTOR_SETTINGS, **stored}
    for key in ("active_hours", "sources", "identity"):
        merged[key] = {**DEFAULT_COLLECTOR_SETTINGS[key], **(stored.get(key) or {})}
    return merged


# ── cursors (metadata table) ─────────────────────────────────────────────


def _cursor_key(source: str) -> str:
    return f"mcp_collector:cursor:{source}"


async def _get_cursor(source: str, lookback_hours: float) -> datetime:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT value FROM metadata WHERE key = ? AND space_id = 'default'",
        (_cursor_key(source),),
    )
    if rows and rows[0]["value"]:
        try:
            return datetime.fromisoformat(json.loads(rows[0]["value"]))
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc) - timedelta(hours=lookback_hours)


async def _set_cursor(source: str, value: datetime) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO metadata (key, value, space_id) VALUES (?, ?, 'default')
           ON CONFLICT(key, space_id) DO UPDATE SET value = excluded.value""",
        (_cursor_key(source), json.dumps(value.isoformat())),
    )
    await db.commit()


_SLACK_THREADS_KEY = "mcp_collector:slack_threads"
_SLACK_CONTEXT_MESSAGES = 8  # earlier messages fetched so a reply makes sense on its own
_SLACK_THREADS_TTL = timedelta(days=14)
_SLACK_THREADS_READ = 15  # most recently active threads re-read per sweep


async def _get_my_slack_threads() -> dict[str, str]:
    """Channel threads the user wrote in during the last 14 days: key -> last seen."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT value FROM metadata WHERE key = ? AND space_id = 'default'", (_SLACK_THREADS_KEY,)
    )
    threads = json.loads(rows[0]["value"]) if rows else {}
    cutoff = (datetime.now(timezone.utc) - _SLACK_THREADS_TTL).isoformat()
    return {k: v for k, v in threads.items() if v >= cutoff}


async def _set_my_slack_threads(threads: dict[str, str]) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO metadata (key, value, space_id) VALUES (?, ?, 'default')
           ON CONFLICT(key, space_id) DO UPDATE SET value = excluded.value""",
        (_SLACK_THREADS_KEY, json.dumps(threads)),
    )
    await db.commit()


# ── claude -p with an MCP read allowlist ─────────────────────────────────


async def _claude_collect(
    source: str, prompt: str, schema: dict, model: str, timeout: float
) -> dict:
    """Run one headless Claude Code turn that may only use `ALLOWED_TOOLS[source]`."""
    from laya.agents.subprocess_helper import AgentProcess
    from laya.llm.agent_backend import weighted_input_tokens
    from laya.llm.client import log_to_audit

    binary = get_agent_binary("claude_code")
    args = [
        binary, "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--allowedTools", ",".join(ALLOWED_TOOLS[source]),
        "--disallowedTools", _DENIED_BUILTINS,
        "--json-schema", json.dumps(schema),
    ]
    tmpdir = tempfile.mkdtemp(prefix="laya-collector-")
    proc = AgentProcess()
    start = time.monotonic()
    try:
        await proc.spawn(args=args, cwd=tmpdir, env=None)

        async def _collect() -> list[str]:
            lines: list[str] = []
            async for line in proc.read_lines(idle_timeout=timeout):
                lines.append(line)
            await proc.wait()
            return lines

        try:
            lines = await asyncio.wait_for(_collect(), timeout=timeout)
        except asyncio.TimeoutError:
            await proc.terminate()
            raise TimeoutError(f"collector '{source}' timed out after {timeout}s")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    data = json.loads("\n".join(lines))
    usage = data.get("usage") or {}
    input_tokens = weighted_input_tokens(usage)
    ok = not data.get("is_error") and data.get("structured_output") is not None
    # Logged as an agent model so the agent usage budget counts the collector too.
    await log_to_audit(
        event_id=None, card_id=None, step=f"collector_{source}",
        model=f"agent/claude_code/{model}",
        input_tokens=input_tokens, output_tokens=usage.get("output_tokens") or 0,
        latency_ms=int((time.monotonic() - start) * 1000), success=ok,
        error=None if ok else str(data.get("result"))[:500],
    )
    if not ok:
        raise RuntimeError(f"collector '{source}' failed: {str(data.get('result'))[:300]}")
    return data["structured_output"]


# ── normalization helpers ────────────────────────────────────────────────


def _event_id(platform: str, native_id: str) -> str:
    return "evt_" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"laya-collector:{platform}:{native_id}"))


def _parse_ts(value: Any) -> datetime:
    """Accept ISO strings, Slack `1700000000.123456` timestamps or epoch numbers."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value or "").strip()
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _make_event(
    *, platform: str, native_id: str, timestamp: datetime, raw_event_type: str,
    actor_name: str, actor_email: str, actor_handle: str | None,
    subject_type: str, subject_id: str, subject_title: str, subject_url: str | None,
    body: str, metadata: dict[str, Any],
) -> LayaEvent:
    return LayaEvent.model_validate({
        "event_id": _event_id(platform, native_id),
        "timestamp": timestamp.isoformat(),
        "source": {
            "platform": platform,
            "connection_id": f"{platform}_mcp",
            "raw_event_type": raw_event_type,
        },
        "actor": {"name": actor_name or "", "email": actor_email or "", "platform_handle": actor_handle},
        "subject": {"type": subject_type, "id": subject_id, "title": subject_title[:300], "url": subject_url},
        "content": {"body": body, "attachments": [], "metadata": metadata},
    })


# ── Slack ────────────────────────────────────────────────────────────────

_SLACK_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "channel_name": {"type": ["string", "null"]},
                    # Free text: search results don't always say public vs private, and
                    # a strict enum made the model refuse the whole answer.
                    "channel_type": {"type": "string", "description": "dm, group_dm, public or private"},
                    "ts": {"type": "string"},
                    "thread_ts": {"type": ["string", "null"]},
                    "user_id": {"type": "string"},
                    "user_name": {"type": "string"},
                    "text": {"type": "string"},
                    "permalink": {"type": "string"},
                    "is_context": {"type": "boolean"},
                },
                "required": ["channel_id", "ts", "user_id", "text", "permalink"],
            },
        },
    },
    "required": ["messages"],
}


def _slack_prompt(me: str, since: datetime, my_threads: list[tuple[str, str]]) -> str:
    after = int(since.timestamp())
    threads = "\n".join(f"   - channel_id={c} thread_ts={t}" for c, t in my_threads)
    thread_step = f"""
4. Read each of these threads with mcp__claude_ai_Slack__slack_read_thread (oldest="{after}")
   and take its messages posted after unix time {after}:
{threads}""" if my_threads else ""
    return f"""You are a data collector. Copy data exactly; do not summarize or judge.
My Slack user id is {me}. Collect Slack messages posted after unix time {after} using
mcp__claude_ai_Slack__slack_search_public_and_private (pass after="{after}",
sort="timestamp", include_bots=false, include_context=false, response_format="detailed",
limit=20; follow the cursor up to 3 pages per query). Run exactly these queries:
1. All direct and group DMs: channel_types="im,mpim", filters="".
2. Messages that mention me: filters="<@{me}>".
3. My own messages anywhere: filters="from:<@{me}>".{thread_step}
Do not add messages from any other search. Deduplicate by channel_id + ts.
Context: for every DM, group DM or thread in which someone other than me posted one of
the messages above, also read the up to {_SLACK_CONTEXT_MESSAGES} messages posted right
before the earliest of them (DMs: mcp__claude_ai_Slack__slack_read_channel with
latest set to that ts; threads: mcp__claude_ai_Slack__slack_read_thread, including the
thread's first message) and return them with is_context=true. All other messages have
is_context=false.
For each message return channel_id, channel_name, channel_type (dm, group_dm, public or
private), ts, thread_ts (the thread root ts, or null if not in a thread), user_id,
user_name, the full message text, its permalink and is_context.
Return {{"messages": []}} if there is nothing."""


def _normalize_slack(m: dict) -> dict:
    """Map whatever channel type the model reported onto dm/group_dm/public/private."""
    raw = (m.get("channel_type") or "").lower().replace("-", "_").replace(" ", "_")
    if raw in ("dm", "im", "direct_message") or str(m.get("channel_id", "")).startswith("D"):
        kind = "dm"
    elif raw in ("group_dm", "mpim", "group"):
        kind = "group_dm"
    elif raw in ("private", "private_channel", "group_channel"):
        kind = "private"
    else:
        kind = "public"
    return {**m, "channel_type": kind}


def _is_mine(m: dict, me: str, my_threads: set[str]) -> bool:
    """Whether a message concerns the user: their DMs, mentions, own messages, threads.

    Applied in code to whatever the collector returns, so a model that collects too
    widely cannot put other people's channel chatter into the feed.
    """
    return (
        m.get("channel_type") in ("dm", "group_dm")
        or m.get("user_id") == me
        or f"<@{me}>" in (m.get("text") or "")
        or _slack_conversation(m) in my_threads
    )


def _slack_conversation(m: dict) -> str:
    """DMs are one conversation per channel; elsewhere one per thread (a root is its own)."""
    if m.get("channel_type") in ("dm", "group_dm"):
        return m["channel_id"]
    return f"{m['channel_id']}:{m.get('thread_ts') or m['ts']}"


def _slack_events(messages: list[dict], me: str) -> list[LayaEvent]:
    """One event per conversation and sweep, not per message.

    The user's latest message becomes a self event (it closes the cards it answers).
    The messages after it, if any, become one event that carries all of them, so a
    burst of five DMs is one card and one router/stager pass instead of five.
    """
    # A DM is titled after the other person, also on the user's own messages in it.
    dm_partner = {
        m["channel_id"]: m.get("user_name")
        for m in messages
        if m.get("channel_type") == "dm" and m.get("user_id") != me and m.get("user_name")
    }
    conversations: dict[str, list[dict]] = {}
    earlier: dict[str, list[dict]] = {}
    for m in messages:
        target = earlier if m.get("is_context") else conversations
        target.setdefault(_slack_conversation(m), []).append(m)

    events = []
    for conversation, msgs in conversations.items():
        msgs.sort(key=lambda m: float(m["ts"]))
        own = [i for i, m in enumerate(msgs) if m.get("user_id") == me]
        if own:
            events.append(_slack_event(conversation, msgs[own[-1]:own[-1] + 1], me, dm_partner, is_self=True))
        unanswered = msgs[own[-1] + 1:] if own else msgs
        if unanswered:
            # What came before (fetched context plus anything new up to the user's
            # last message) goes into the body so the card is readable on its own.
            before = sorted(earlier.get(conversation, []), key=lambda m: float(m["ts"]))
            before += msgs[:own[-1] + 1] if own else []
            events.append(_slack_event(conversation, unanswered, me, dm_partner, is_self=False, earlier=before))
    return events


def _slack_line(m: dict, me: str) -> str:
    who = "Me" if m.get("user_id") == me else (m.get("user_name") or m.get("user_id"))
    when = _parse_ts(m["ts"]).astimezone(safe_zoneinfo("Europe/Berlin")).strftime("%a %d.%m %H:%M")
    return f"[{when}] {who}: {m.get('text', '')}"


def _slack_event(
    conversation: str, msgs: list[dict], me: str, dm_partner: dict, *, is_self: bool,
    earlier: list[dict] | None = None,
) -> LayaEvent:
    first, last = msgs[0], msgs[-1]
    channel_type = last.get("channel_type", "public")
    name = (last.get("channel_name") or last["channel_id"]).lstrip("#")
    partner = dm_partner.get(last["channel_id"]) or (name if name != last["channel_id"] else "")
    title = f"DM with {partner or 'direct message'}" if channel_type == "dm" else f"#{name}"
    body = last.get("text", "") if len(msgs) == 1 and not earlier else "\n".join(
        f"{m.get('user_name') or m.get('user_id')}: {m.get('text', '')}" for m in msgs
    )
    if earlier:
        body = (
            "Earlier in the conversation:\n"
            + "\n".join(_slack_line(m, me) for m in earlier)
            + "\n\nNew:\n"
            + "\n".join(_slack_line(m, me) for m in msgs)
        )
    thread_ts = last.get("thread_ts") or None
    return _make_event(
        platform="slack", native_id=f"{last['channel_id']}:{last['ts']}",
        timestamp=_parse_ts(last["ts"]),
        raw_event_type="thread_reply" if thread_ts else "message_received",
        actor_name=last.get("user_name", ""), actor_email="", actor_handle=last.get("user_id"),
        subject_type="thread", subject_id=conversation, subject_title=title,
        # The first unanswered message is where the user picks up the conversation.
        subject_url=first.get("permalink"), body=body,
        metadata={
            "slack_channel": name,
            "slack_channel_name": name if channel_type not in ("dm", "group_dm") else None,
            "slack_channel_id": last["channel_id"],
            "slack_channel_type": "dm" if channel_type in ("dm", "group_dm") else channel_type,
            # Same convention as the n8n workflow: a root message is its own thread.
            "slack_thread_ts": thread_ts or last["ts"],
            "slack_is_mention": any(f"<@{me}>" in (m.get("text") or "") for m in msgs),
            "slack_message_count": len(msgs),
            "is_self": is_self,
        },
    )


# ── Gmail ────────────────────────────────────────────────────────────────

_GMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "date": {"type": "string"},
                    "from_name": {"type": "string"},
                    "from_email": {"type": "string"},
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "url": {"type": "string"},
                    "kind": {"type": "string", "enum": ["email", "meeting_notes"]},
                },
                "required": ["message_id", "thread_id", "date", "from_email", "subject", "body", "url", "kind"],
            },
        },
    },
    "required": ["messages"],
}


def _gmail_prompt(since: datetime, exclude: list[str], my_name: str) -> str:
    after = int(since.timestamp())
    excluded = " ".join(f"-from:{s}" for s in exclude)
    return f"""You are a data collector. Copy data exactly; do not judge relevance.
Use mcp__claude_ai_Gmail__search_threads with query
"after:{after} -category:promotions -category:social {excluded}" (pageSize 25, up to
2 pages). This includes mails I sent. For every thread found, call
mcp__claude_ai_Gmail__get_thread and return each message dated after unix time {after}:
message_id, thread_id, date (ISO 8601), from_name, from_email, to, cc, subject, the
plain-text body (newest part only, without quoted history, at most 2000 characters),
labels and the message's viewUrl as url. Set kind="email".
Mails from gemini-notes@google.com are meeting notes: set kind="meeting_notes". Open
the notes document the mail links to with mcp__claude_ai_Google_Drive__read_file_content
(the notes, not the transcript; fall back to the mail text if it can't be read). Put into
body: the meeting title and date; "Action items for {my_name}:" with every action item or
next step assigned to {my_name} or to everyone, one per line, with its due date if given;
"Decisions:" with the decisions taken, one per line; "Waiting on others:" with items
{my_name} asked others for. Leave a heading out when it has no lines. If nothing concerns
{my_name}, body is "No action items for {my_name}." Use the document's link as url.
Return {{"messages": []}} if there is nothing."""


def _gmail_events(messages: list[dict], my_emails: set[str]) -> list[LayaEvent]:
    """One event per thread and sweep, split at the user's latest mail like Slack."""
    threads: dict[str, list[dict]] = {}
    for m in messages:
        threads.setdefault(m["thread_id"], []).append(m)

    events = []
    for msgs in threads.values():
        msgs.sort(key=lambda m: _parse_ts(m["date"]))
        own = [i for i, m in enumerate(msgs) if (m.get("from_email") or "").lower() in my_emails]
        if own:
            events.append(_gmail_event(msgs[own[-1]:own[-1] + 1], is_self=True))
        unanswered = msgs[own[-1] + 1:] if own else msgs
        if unanswered:
            events.append(_gmail_event(unanswered, is_self=False))
    return events


def _gmail_event(msgs: list[dict], *, is_self: bool) -> LayaEvent:
    first, last = msgs[0], msgs[-1]
    sender = (last.get("from_email") or "").lower()
    is_notes = last.get("kind") == "meeting_notes"
    body = last.get("body", "") if len(msgs) == 1 else "\n\n".join(
        f"From {m.get('from_name') or m.get('from_email')} ({m['date']}):\n{m.get('body', '')}"
        for m in msgs
    )
    return _make_event(
        platform="gmail", native_id=last["message_id"], timestamp=_parse_ts(last["date"]),
        raw_event_type="meeting_notes" if is_notes else "new_email",
        actor_name=last.get("from_name", ""), actor_email=sender, actor_handle=None,
        subject_type="meeting" if is_notes else "email_thread",
        subject_id=last["thread_id"], subject_title=last.get("subject", ""),
        subject_url=first.get("url"), body=body,
        metadata={
            "gmail_thread_id": last["thread_id"],
            "gmail_labels": last.get("labels", []),
            "gmail_from": sender,
            "gmail_to": last.get("to", []),
            "gmail_cc": last.get("cc", []),
            "gmail_message_count": len(msgs),
            "is_self": is_self,
        },
    )


# ── Linear ───────────────────────────────────────────────────────────────

_LINEAR_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "created_at": {"type": "string"},
                    "kind": {"type": "string", "enum": [
                        "comment", "mention", "assigned", "status_changed", "issue_created", "other",
                    ]},
                    "issue_identifier": {"type": "string"},
                    "issue_title": {"type": "string"},
                    "issue_url": {"type": "string"},
                    "issue_status": {"type": "string"},
                    "actor_name": {"type": "string"},
                    "actor_email": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "created_at", "kind", "issue_identifier", "issue_title", "issue_url", "text"],
            },
        },
    },
    "required": ["items"],
}


def _linear_prompt(since: datetime) -> str:
    iso = since.isoformat()
    return f"""You are a data collector. Copy data exactly; do not judge relevance.
1. Call mcp__linear-server__get_notifications and take every notification created after
   {iso} (read or unread): mentions, comments, assignments, status changes. For each
   return id (the notification id), created_at, kind, issue_identifier, issue_title,
   issue_url, issue_status, actor_name, actor_email and text: the comment body for
   comments and mentions, otherwise one sentence describing the change.
2. Call mcp__linear-server__list_issues with assignee="me", updatedAt="{iso}" and add
   each issue not already covered by step 1 with kind="assigned" (id = "issue:" +
   identifier + ":" + its updatedAt, created_at = its updatedAt, text = its status and
   the first 500 characters of its description).
Return {{"items": []}} if there is nothing."""


_LINEAR_RESOLVED = {"done", "canceled", "cancelled", "duplicate"}
_REVIEW_REQUEST = re.compile(r"requested\b.*\breview", re.IGNORECASE)


def _linear_events(items: list[dict], my_emails: set[str]) -> list[LayaEvent]:
    events = []
    for it in items:
        # Review requests (Linear's PR reviews, mostly to a team) and items without an
        # issue are not asks to the user.
        if not it.get("issue_identifier") or _REVIEW_REQUEST.search(it.get("text") or ""):
            continue
        actor_email = (it.get("actor_email") or "").lower()
        resolved = (it.get("issue_status") or "").strip().lower() in _LINEAR_RESOLVED
        events.append(_make_event(
            platform="linear", native_id=it["id"], timestamp=_parse_ts(it["created_at"]),
            raw_event_type=f"issue_{it['kind']}", actor_name=it.get("actor_name", ""),
            actor_email=actor_email, actor_handle=None, subject_type="ticket",
            subject_id=it["issue_identifier"],
            subject_title=f"{it['issue_identifier']}: {it['issue_title']}",
            subject_url=it.get("issue_url"), body=it.get("text", ""),
            metadata={
                "linear_status": it.get("issue_status"),
                # A finished issue needs nothing more from the user: close its cards.
                "is_self": actor_email in my_emails or resolved,
                "resolved_in_source": resolved,
            },
        ))
    return events


# ── Notion ───────────────────────────────────────────────────────────────

_NOTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "created_at": {"type": "string"},
                    "kind": {"type": "string", "enum": ["comment", "mention"]},
                    "page_id": {"type": "string"},
                    "page_title": {"type": "string"},
                    "page_url": {"type": "string"},
                    "discussion_id": {"type": ["string", "null"]},
                    "author_name": {"type": "string"},
                    "author_email": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "created_at", "kind", "page_id", "page_title", "page_url", "author_name", "text"],
            },
        },
    },
    "required": ["items"],
}


def _notion_prompt(since: datetime, my_name: str) -> str:
    day = since.strftime("%Y-%m-%d")
    iso = since.isoformat()
    return f"""You are a data collector. Copy data exactly; do not summarize or judge.
I am {my_name} (call mcp__notion__notion-get-self for my user id and email).
1. mcp__notion__notion-search with query "", filters {{"created_by_user_ids": ["me"],
   "last_edited_date_range": {{"start_date": "{day}"}}}}, sort "last_edited", page_size 25.
2. mcp__notion__notion-search with query "{my_name}", filters {{"last_edited_date_range":
   {{"start_date": "{day}"}}}}, sort "last_edited", page_size 15.
For every page found, call mcp__notion__notion-get-comments and return each comment
created after {iso}: kind="comment", id = the comment id, discussion_id, author_name,
author_email, text, created_at, page_id, page_title, page_url.
For pages from step 2 that @-mention me in their content (fetch them with
mcp__notion__notion-fetch), return one item kind="mention" with id = page id + ":" +
the page's last edited time, created_at = that time, author = who last edited it, and
text = the paragraph that mentions me.
Return {{"items": []}} if there is nothing."""


def _notion_events(items: list[dict], my_emails: set[str]) -> list[LayaEvent]:
    events = []
    for it in items:
        author_email = (it.get("author_email") or "").lower()
        conversation = f"{it['page_id']}:{it['discussion_id']}" if it.get("discussion_id") else it["page_id"]
        events.append(_make_event(
            platform="notion", native_id=it["id"], timestamp=_parse_ts(it["created_at"]),
            raw_event_type=f"page_{it['kind']}", actor_name=it.get("author_name", ""),
            actor_email=author_email, actor_handle=None, subject_type="page",
            subject_id=conversation, subject_title=it["page_title"],
            subject_url=it.get("page_url"), body=it.get("text", ""),
            metadata={"notion_page_id": it["page_id"], "is_self": author_email in my_emails},
        ))
    return events


# ── GitHub (gh CLI, no LLM) ──────────────────────────────────────────────


# Notifications that ask nothing of the user personally: review requests (mostly
# CODEOWNERS team requests), state changes and CI runs.
_GITHUB_SKIP_REASONS = {"review_requested", "state_change", "ci_activity", "security_alert"}


async def _gh_json(*args: str) -> Any:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {err.decode()[:300]}")
    return json.loads(out or b"null")


def _html_url(api_url: str | None) -> str | None:
    """https://api.github.com/repos/o/r/pulls/5 → https://github.com/o/r/pull/5"""
    if not api_url:
        return None
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    return url.replace("/pulls/", "/pull/")


async def _is_own_pr(api_url: str | None, me: str) -> bool:
    if not api_url:
        return False
    try:
        pr = await _gh_json("api", api_url)
    except RuntimeError:
        return False
    return ((pr or {}).get("user") or {}).get("login", "").lower() == me.lower()


async def _collect_github(since: datetime, settings: dict) -> list[LayaEvent]:
    me = settings["identity"].get("github_login") or (await _gh_json("api", "user"))["login"]
    orgs = {o.lower() for o in settings.get("github_orgs") or []}
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    events: list[LayaEvent] = []

    notifications = await _gh_json(
        "api", "--paginate", "--slurp",
        f"notifications?all=true&participating=true&since={since_iso}",
    )
    for page in notifications or []:
        for n in page:
            repo = n["repository"]["full_name"]
            if orgs and n["repository"]["owner"]["login"].lower() not in orgs:
                continue
            if n.get("reason") in _GITHUB_SKIP_REASONS:
                continue
            subject = n["subject"]
            number = (subject.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            # Code review of other people's PRs runs through CodeRabbit, so their PRs
            # only matter when someone @-mentions the user there. The user's own PRs
            # (and issues) keep every comment and review from a person.
            if subject.get("type") == "PullRequest" and n.get("reason") != "mention":
                if n.get("reason") != "author" and not await _is_own_pr(subject.get("url"), me):
                    continue
            body, actor, url = "", "", _html_url(subject.get("url"))
            if subject.get("latest_comment_url") and subject["latest_comment_url"] != subject.get("url"):
                try:
                    comment = await _gh_json("api", subject["latest_comment_url"])
                    body = comment.get("body") or ""
                    actor = (comment.get("user") or {}).get("login", "")
                    url = comment.get("html_url") or url
                    # Bot comments (CI summaries, review bots) carry no ask from a person.
                    if actor.endswith("[bot]"):
                        continue
                except RuntimeError as e:
                    log.debug("collector_github_comment_failed", error=str(e))
            events.append(_make_event(
                platform="github", native_id=f"notification:{n['id']}:{n['updated_at']}",
                timestamp=_parse_ts(n["updated_at"]), raw_event_type=n.get("reason", "notification"),
                actor_name=actor, actor_email="", actor_handle=actor or None,
                subject_type="pull_request" if subject.get("type") == "PullRequest" else "ticket",
                subject_id=f"{repo}#{number}", subject_title=f"{repo}#{number}: {subject.get('title', '')}",
                subject_url=url, body=body or f"{n.get('reason', '')}: {subject.get('title', '')}",
                metadata={"github_repo": repo, "github_reason": n.get("reason"), "is_self": actor == me},
            ))

    # The user's merged PRs close every open card about that PR (self events).
    org_filter = " ".join(f"org:{o}" for o in sorted(orgs))
    merged = await _gh_json(
        "api", "-X", "GET", "search/issues",
        "-f", f"q=is:pr is:merged author:{me} merged:>={since_iso} {org_filter}".strip(),
        "-f", "per_page=50",
    )
    for pr in (merged or {}).get("items", []):
        repo = pr["repository_url"].replace("https://api.github.com/repos/", "")
        merged_at = (pr.get("pull_request") or {}).get("merged_at") or pr["closed_at"]
        events.append(_make_event(
            platform="github", native_id=f"merged:{repo}#{pr['number']}",
            timestamp=_parse_ts(merged_at), raw_event_type="pr_merged",
            actor_name=me, actor_email="", actor_handle=me, subject_type="pull_request",
            subject_id=f"{repo}#{pr['number']}", subject_title=f"{repo}#{pr['number']}: {pr['title']}",
            subject_url=pr["html_url"], body=f"Merged by {me}.",
            metadata={"github_repo": repo, "is_self": True},
        ))
    return events


# ── sweep ────────────────────────────────────────────────────────────────


async def _collect_source(source: str, since: datetime, settings: dict) -> list[LayaEvent]:
    identity = settings["identity"]
    my_emails = {e.lower() for e in identity.get("emails") or []}
    model, timeout = settings["model"], float(settings["timeout_seconds"])

    if source == "github":
        return await _collect_github(since, settings)
    if source == "slack":
        me = identity.get("slack_user_id")
        if not me:
            raise RuntimeError("identity.slack_user_id is not set")
        known = await _get_my_slack_threads()
        recent = sorted(known.items(), key=lambda kv: kv[1], reverse=True)[:_SLACK_THREADS_READ]
        to_read = [tuple(key.split(":", 1)) for key, _ in recent]
        prompt = _slack_prompt(me, since, to_read)
        out = await _claude_collect("slack", prompt, _SLACK_SCHEMA, model, timeout)
        messages = [_normalize_slack(m) for m in out.get("messages", [])]
        # Threads the user writes in become theirs; replies there count from now on.
        now = datetime.now(timezone.utc).isoformat()
        for m in messages:
            if m.get("user_id") == me and not m.get("is_context") and m.get("channel_type") not in ("dm", "group_dm"):
                known[_slack_conversation(m)] = now
        await _set_my_slack_threads(known)
        new = [m for m in messages if not m.get("is_context") and _is_mine(m, me, set(known))]
        kept = {_slack_conversation(m) for m in new}
        context = [m for m in messages if m.get("is_context") and _slack_conversation(m) in kept]
        return _slack_events(new + context, me)
    if source == "gmail":
        from laya.config import get_self_user

        my_name = (get_self_user() or {}).get("name") or "me"
        prompt = _gmail_prompt(since, settings["gmail_exclude_senders"], my_name)
        out = await _claude_collect("gmail", prompt, _GMAIL_SCHEMA, model, timeout)
        return _gmail_events(out.get("messages", []), my_emails)
    if source == "notion":
        from laya.config import get_self_user

        my_name = (get_self_user() or {}).get("name") or "me"
        out = await _claude_collect("notion", _notion_prompt(since, my_name), _NOTION_SCHEMA, model, timeout)
        return _notion_events(out.get("items", []), my_emails)
    if source == "linear":
        out = await _claude_collect("linear", _linear_prompt(since), _LINEAR_SCHEMA, model, timeout)
        return _linear_events(out.get("items", []), my_emails)
    raise ValueError(f"unknown source {source!r}")


async def run_sweep(sources: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Collect every enabled source once and store the events. Returns per-source status."""
    from laya.api.events import receive_event
    from laya.pipeline.agent_budget import is_agent_budget_paused

    settings = get_collector_settings()
    wanted = [s for s in (sources or SOURCES) if settings["sources"].get(s)]
    async with _sweep_lock:
        if await is_agent_budget_paused():
            log.info("collector_skipped_budget_paused")
            return {s: {"ok": False, "error": "agent budget paused"} for s in wanted}
        for source in wanted:
            started = datetime.now(timezone.utc)
            t0 = time.monotonic()
            try:
                since = await _get_cursor(source, settings["initial_lookback_hours"])
                # Overlap the window a little: duplicates are dropped by event id.
                events = await _collect_source(source, since - timedelta(minutes=5), settings)
                for event in events:
                    await receive_event(event)
                await _set_cursor(source, started)
                _status[source] = {"ok": True, "events": len(events), "error": None}
                log.info("collector_source_done", source=source, events=len(events))
            except Exception as e:  # one failing source must not stop the others
                _status[source] = {"ok": False, "events": 0, "error": str(e)[:500]}
                log.warning("collector_source_failed", source=source, error=str(e))
            _status[source].update(at=started.isoformat(), duration_ms=int((time.monotonic() - t0) * 1000))
            _last_sweep[source] = started
    return {s: _status.get(s, {}) for s in wanted}


def get_collector_status() -> dict[str, Any]:
    settings = get_collector_settings()
    return {
        "enabled": settings["enabled"],
        "interval_minutes": settings["interval_minutes"],
        "sources": {s: {"enabled": bool(settings["sources"].get(s)), **_status.get(s, {})} for s in SOURCES},
    }


def _within_active_hours(settings: dict, now: datetime) -> bool:
    hours = settings["active_hours"]
    local = now.astimezone(safe_zoneinfo(settings["timezone"]))
    if local.weekday() not in hours.get("days", []):
        return False
    return hours["start"] <= local.strftime("%H:%M") < hours["end"]


async def _collector_loop() -> None:
    while True:
        try:
            settings = get_collector_settings()
            now = datetime.now(timezone.utc)
            interval = timedelta(minutes=max(5, int(settings["interval_minutes"])))
            if settings["enabled"] and _within_active_hours(settings, now):
                due = []
                for source in SOURCES:
                    if not settings["sources"].get(source):
                        continue
                    # The cursor survives restarts; the in-memory time does not. A
                    # restart must not trigger a fresh sweep of every source.
                    last = _last_sweep.get(source) or await _get_cursor(source, interval.total_seconds() / 3600 + 1)
                    if now - last >= interval:
                        due.append(source)
                if due:
                    await run_sweep(due)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("collector_loop_error", error=str(e))
        await asyncio.sleep(60)


def start_collector() -> None:
    global _collector_task
    if _collector_task is None or _collector_task.done():
        _collector_task = asyncio.create_task(_collector_loop(), name="mcp_collector")
        log.info("collector_started")


def stop_collector() -> None:
    global _collector_task
    if _collector_task and not _collector_task.done():
        _collector_task.cancel()
    _collector_task = None
