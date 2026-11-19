# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

""""Still open?" re-check of open cards.

A sweep only sees what happened inside its window. When the user answers an
older conversation somewhere the sweep does not look (a thread outside the
remembered ones, a reply from the phone before the cursor moved) or the item
is closed in its source without a notification, the card would stay open.

After each sweep, open cards (`ready`, `requires_approval`) of this platform
that are older than `recheck_after_hours`, with no newer event in their
entity, are re-checked in their source:

- slack: a later message by the user in the thread / DM;
- gmail: a later mail from the user in the thread;
- linear: the issue is Done / Canceled, or the user commented later;
- github: the pull request / issue is merged or closed, or the user commented later.

An answered item yields an event with `resolved_in_source`, which concludes
the entity's cards. Each entity is checked at most once per
`recheck_after_hours`, and at most `RECHECK_LIMIT` entities per sweep.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from laya.claude_code import store
from laya.claude_code.cli import ClaudeCodeError, run_gh
from laya.claude_code.platforms.common import (
    connection_id,
    make_event,
    my_emails,
    parse_ts,
    run_turn,
)
from laya.claude_code.platforms.linear import RESOLVED_STATUSES
from laya.db.sqlite import get_db
from laya.db.timeutil import db_ts
from laya.models.event import LayaEvent

log = structlog.get_logger()

RECHECK_LIMIT = 5
_STATE_DAYS = 7
_STATE_PREFIX = "recheck:"
_RECHECKABLE = ("slack", "gmail", "linear", "github")


async def due_entities(platform: str, after_hours: float, limit: int = RECHECK_LIMIT) -> list[dict[str, Any]]:
    """Open cards' entities that are due for a re-check, oldest first."""
    db = await get_db()
    cutoff = db_ts(datetime.now(timezone.utc) - timedelta(hours=after_hours))
    rows = await db.execute_fetchall(
        """SELECT c.entity_id, e.event_id, e.timestamp, e.subject_type, e.subject_id,
                  e.subject_url, e.content_metadata
           FROM action_cards c JOIN events e ON e.event_id = c.event_id
           WHERE e.source_connection_id = ?
             AND c.status IN ('ready', 'requires_approval')
             AND (c.status_set_by IS NULL OR c.status_set_by != 'user')
             AND c.created_at <= ?
             AND NOT EXISTS (
                 SELECT 1 FROM events e2
                 WHERE e2.source_platform = e.source_platform
                   AND e2.subject_type = e.subject_type
                   AND e2.subject_id = e.subject_id
                   AND e2.timestamp > e.timestamp)
           ORDER BY c.created_at ASC""",
        (connection_id(platform), cutoff),
    )
    checked = await store.get_state(platform, _STATE_PREFIX)
    recent = datetime.now(timezone.utc) - timedelta(hours=after_hours)
    # Marks older than a week no longer matter: a card that old is due anyway.
    expired = datetime.now(timezone.utc) - timedelta(days=_STATE_DAYS)
    await store.delete_state(platform, [k for k, v in checked.items() if parse_ts(v) < expired])
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        entity = r["entity_id"]
        if entity in seen:
            continue
        seen.add(entity)
        last = checked.get(_STATE_PREFIX + entity)
        if last and parse_ts(last) > recent:
            continue
        try:
            metadata = json.loads(r["content_metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        out.append({
            "entity_id": entity,
            "event_id": r["event_id"],
            "timestamp": parse_ts(r["timestamp"].replace(" ", "T")),
            "subject_type": r["subject_type"],
            "subject_id": r["subject_id"],
            "subject_url": r["subject_url"],
            "metadata": metadata,
        })
        if len(out) >= limit:
            break
    return out


def resolution_event(
    platform: str, entity: dict[str, Any], *, answered_at: datetime | None, url: str | None,
    by_user: dict[str, str] | None, note: str,
) -> LayaEvent:
    """The closing event for an entity found answered or resolved."""
    when = answered_at or datetime.now(timezone.utc)
    by_user = by_user or {}
    return make_event(
        platform=platform,
        native_id=f"recheck:{entity['entity_id']}:{when.isoformat()}",
        timestamp=when,
        raw_event_type="recheck_resolved",
        actor_name=by_user.get("name") or "Laya re-check",
        actor_email=by_user.get("email") or "",
        actor_handle=by_user.get("handle"),
        subject_type=entity["subject_type"],
        subject_id=entity["subject_id"],
        subject_title=entity.get("title") or entity["subject_id"],
        subject_url=url or entity.get("subject_url"),
        body=note,
        metadata={**_platform_keys(entity["metadata"]), "resolved_in_source": True, "recheck": True},
    )


def _platform_keys(metadata: dict[str, Any]) -> dict[str, Any]:
    keep = ("slack_channel_id", "slack_channel_name", "slack_channel_type", "slack_thread_ts",
            "gmail_thread_id", "github_repo", "linear_status", "notion_page_id")
    return {k: metadata[k] for k in keep if k in metadata}


# ── GitHub (gh) ──────────────────────────────────────────────────────────


async def _check_github(source: dict[str, Any], entities: list[dict[str, Any]]) -> list[LayaEvent]:
    me = ((source.get("identity") or {}).get("github_login") or "").lower()
    events = []
    for entity in entities:
        repo, _, number = entity["subject_id"].rpartition("#")
        if not repo or not number.isdigit():
            continue
        kind = "pulls" if entity["subject_type"] == "pull_request" else "issues"
        try:
            item = await run_gh("api", f"repos/{repo}/{kind}/{number}") or {}
        except ClaudeCodeError as e:
            log.debug("claude_code_recheck_github_failed", entity=entity["entity_id"], error=str(e))
            continue
        if item.get("state") == "closed":
            merged = bool(item.get("merged_at"))
            when = item.get("merged_at") or item.get("closed_at")
            events.append(resolution_event(
                "github", entity, answered_at=parse_ts(when) if when else None,
                url=item.get("html_url"), by_user=None,
                note=f"{'Merged' if merged else 'Closed'} on GitHub.",
            ))
            continue
        since = entity["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            comments = await run_gh("api", f"repos/{repo}/issues/{number}/comments?since={since}") or []
        except ClaudeCodeError:
            comments = []
        mine = [c for c in comments
                if ((c.get("user") or {}).get("login") or "").lower() == me
                and parse_ts(c["created_at"]) > entity["timestamp"]]
        if me and mine:
            c = mine[-1]
            events.append(resolution_event(
                "github", entity, answered_at=parse_ts(c["created_at"]), url=c.get("html_url"),
                by_user={"handle": me, "name": me}, note="You commented on GitHub.",
            ))
    return events


# ── MCP platforms (one claude turn per sweep) ────────────────────────────

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "answered": {"type": "boolean"},
                    "resolved": {"type": "boolean"},
                    "status": {"type": ["string", "null"]},
                    "at": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                },
                "required": ["key", "answered"],
            },
        },
    },
    "required": ["items"],
}


def slack_prompt(tools: dict[str, str], me: str, entities: list[dict[str, Any]]) -> str:
    lines = []
    for e in entities:
        md = e["metadata"]
        channel = md.get("slack_channel_id") or e["subject_id"].split(":", 1)[0]
        after = e["timestamp"].timestamp()
        if md.get("slack_channel_type") in ("dm", "group_dm") or ":" not in e["subject_id"]:
            lines.append(f"- key={e['entity_id']}: {tools['read_channel']} channel_id={channel} oldest=\"{after}\"")
        else:
            thread_ts = e["subject_id"].split(":", 1)[1]
            lines.append(f"- key={e['entity_id']}: {tools['read_thread']} channel_id={channel} "
                         f"thread_ts={thread_ts} oldest=\"{after}\"")
    return f"""You check whether I answered these Slack conversations. My Slack user id is {me}.
For each line, read the messages with the given tool and parameters and report
answered=true when a message from user {me} was posted after the given oldest time;
then at = that message's ts and url = its permalink. Otherwise answered=false.
{chr(10).join(lines)}
Return one item per key."""


def gmail_prompt(tools: dict[str, str], emails: set[str], entities: list[dict[str, Any]]) -> str:
    lines = [f"- key={e['entity_id']}: thread_id={e['metadata'].get('gmail_thread_id') or e['subject_id']} "
             f"after {e['timestamp'].isoformat()}" for e in entities]
    return f"""You check whether I answered these Gmail threads. My addresses: {', '.join(sorted(emails))}.
For each line call {tools['get_thread']} and report answered=true when the thread has a
message from one of my addresses dated after the given time; then at = its date (ISO 8601)
and url = its web link. Otherwise answered=false.
{chr(10).join(lines)}
Return one item per key."""


def linear_prompt(tools: dict[str, str], emails: set[str], entities: list[dict[str, Any]]) -> str:
    comments = f" and {tools['list_comments']}" if tools.get("list_comments") else ""
    lines = [f"- key={e['entity_id']}: issue {e['subject_id']} after {e['timestamp'].isoformat()}"
             for e in entities]
    return f"""You check the state of these Linear issues. My addresses: {', '.join(sorted(emails))}.
For each line call {tools['get_issue']}{comments}. Return status = the issue's current status
name, resolved=true when it is Done, Canceled or Duplicate, and answered=true when I
commented after the given time (then at = the comment time and url = its link).
{chr(10).join(lines)}
Return one item per key."""


async def _check_mcp(platform: str, source: dict[str, Any], entities: list[dict[str, Any]]) -> list[LayaEvent]:
    tools = source.get("tools") or {}
    identity = source.get("identity") or {}
    emails = my_emails(source)
    if platform == "slack":
        me = identity.get("slack_user_id")
        if not me:
            return []
        prompt = slack_prompt(tools, me, entities)
        by_user = {"handle": me, "name": identity.get("name") or ""}
        note = "You replied in Slack."
    elif platform == "gmail":
        prompt = gmail_prompt(tools, emails, entities)
        by_user = {"email": identity.get("email") or next(iter(sorted(emails)), ""),
                   "name": identity.get("name") or ""}
        note = "You replied by email."
    else:
        prompt = linear_prompt(tools, emails, entities)
        by_user = {"email": identity.get("email") or next(iter(sorted(emails)), ""),
                   "name": identity.get("name") or ""}
        note = "You commented in Linear."

    out = await run_turn(source, platform, prompt, _ANSWER_SCHEMA)
    by_key = {e["entity_id"]: e for e in entities}
    events = []
    for item in out.get("items", []):
        entity = by_key.get(item.get("key") or "")
        if entity is None:
            continue
        at = None
        if item.get("at"):
            try:
                at = parse_ts(item["at"])
            except ValueError:
                at = None
        resolved = platform == "linear" and (
            item.get("resolved") or (item.get("status") or "").strip().lower() in RESOLVED_STATUSES
        )
        if resolved:
            events.append(resolution_event(
                platform, entity, answered_at=None, url=item.get("url"), by_user=None,
                note=f"Linear status: {item.get('status') or 'resolved'}.",
            ))
        elif item.get("answered") and (at is None or at > entity["timestamp"]):
            events.append(resolution_event(
                platform, entity, answered_at=at, url=item.get("url"), by_user=by_user, note=note,
            ))
    return events


async def recheck(platform: str, source: dict[str, Any]) -> list[LayaEvent]:
    """Re-check the due open cards of a platform; returns the closing events."""
    if platform not in _RECHECKABLE:
        return []
    config = source.get("config") or {}
    entities = await due_entities(platform, float(config.get("recheck_after_hours", 6)))
    if not entities:
        return []
    if platform == "github":
        events = await _check_github(source, entities)
    else:
        events = await _check_mcp(platform, source, entities)
    now = store.now_iso()
    for entity in entities:
        await store.set_state(platform, _STATE_PREFIX + entity["entity_id"], now)
    log.info("claude_code_recheck_done", platform=platform, checked=len(entities), resolved=len(events))
    return events
