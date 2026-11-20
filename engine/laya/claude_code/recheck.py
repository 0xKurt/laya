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


# Open cards a re-check may conclude: waiting on the user (`ready`,
# `requires_approval`), not reopened by the user, and their event is the
# latest of its entity (a newer event means the conversation moved on and the
# sweep has seen it). Shared with related.py (My work refreshes).
_OPEN_CARDS_SQL = """SELECT c.card_id, c.header, c.summary, c.source_ref, c.source_url, c.created_at,
                  c.entity_id, e.event_id, e.timestamp, e.source_platform, e.subject_type,
                  e.subject_id, e.subject_url, e.content_metadata
           FROM action_cards c JOIN events e ON e.event_id = c.event_id
           WHERE c.status IN ('ready', 'requires_approval')
             AND (c.status_set_by IS NULL OR c.status_set_by != 'user')
             AND NOT EXISTS (
                 SELECT 1 FROM events e2
                 WHERE e2.source_platform = e.source_platform
                   AND e2.subject_type = e.subject_type
                   AND e2.subject_id = e.subject_id
                   AND e2.timestamp > e.timestamp)
             {extra}
           ORDER BY c.created_at ASC"""


async def open_card_rows(extra: str = "", params: tuple = ()) -> list[Any]:
    """The open cards a re-check may conclude, oldest first; `extra` adds `AND ...` filters."""
    db = await get_db()
    return await db.execute_fetchall(_OPEN_CARDS_SQL.format(extra=extra), params)


def entity_of(row: Any) -> dict[str, Any]:
    """The re-check entity of an open card row."""
    try:
        metadata = json.loads(row["content_metadata"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    created = row["created_at"]
    return {
        "entity_id": row["entity_id"],
        "event_id": row["event_id"],
        "timestamp": parse_ts(row["timestamp"].replace(" ", "T")),
        "created_at": parse_ts(created.replace(" ", "T")) if created else None,
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "subject_url": row["subject_url"],
        "metadata": metadata,
    }


async def due_entities(platform: str, after_hours: float, limit: int = RECHECK_LIMIT) -> list[dict[str, Any]]:
    """Open cards' entities that are due for a re-check, oldest first."""
    cutoff = db_ts(datetime.now(timezone.utc) - timedelta(hours=after_hours))
    rows = await open_card_rows("AND e.source_connection_id = ? AND c.created_at <= ?",
                                (connection_id(platform), cutoff))
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
        out.append(entity_of(r))
        if len(out) >= limit:
            break
    return out


async def mark_checked(platform: str, entity_ids: list[str]) -> None:
    """Record a re-check of these entities, so the sweep's re-check skips them
    for `recheck_after_hours`."""
    now = store.now_iso()
    for entity_id in entity_ids:
        await store.set_state(platform, _STATE_PREFIX + entity_id, now)


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


async def github_item(repo: str, kind: str, number: str) -> dict[str, Any] | None:
    """The pull request (`kind="pulls"`) or issue from the GitHub API, or None."""
    try:
        return await run_gh("api", f"repos/{repo}/{kind}/{number}") or {}
    except ClaudeCodeError as e:
        log.debug("claude_code_recheck_github_failed", item=f"{repo}#{number}", error=str(e))
        return None


def github_closed(item: dict[str, Any]) -> tuple[datetime | None, str] | None:
    """(closed time, note) for a merged or closed pull request / issue, else None."""
    if item.get("state") != "closed":
        return None
    merged = bool(item.get("merged_at"))
    when = item.get("merged_at") or item.get("closed_at")
    return (parse_ts(when) if when else None), f"{'Merged' if merged else 'Closed'} on GitHub."


def own_resolution_time(entity: dict[str, Any], when: datetime | None) -> datetime:
    """When an item's own entity counts as resolved.

    A card can come from an event read just after the item was merged or
    closed (the sweep saw the PR's last update). A conclusion at the merge
    time would then be older than the card and close nothing, so the time is
    never before the card's event or its creation.
    """
    times = [t for t in (when, entity.get("timestamp"), entity.get("created_at")) if t]
    return max(times) if times else datetime.now(timezone.utc)


async def _check_github(source: dict[str, Any], entities: list[dict[str, Any]]) -> list[LayaEvent]:
    me = ((source.get("identity") or {}).get("github_login") or "").lower()
    events = []
    for entity in entities:
        repo, _, number = entity["subject_id"].rpartition("#")
        if not repo or not number.isdigit():
            continue
        kind = "pulls" if entity["subject_type"] == "pull_request" else "issues"
        item = await github_item(repo, kind, number)
        if item is None:
            continue
        closed = github_closed(item)
        if closed:
            when, note = closed
            events.append(resolution_event(
                "github", entity, answered_at=own_resolution_time(entity, when),
                url=item.get("html_url"), by_user=None, note=note,
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


async def check_entities(platform: str, source: dict[str, Any], entities: list[dict[str, Any]]) -> list[LayaEvent]:
    """Re-check these entities now and mark them checked; returns the closing events."""
    if platform not in _RECHECKABLE or not entities:
        return []
    if platform == "github":
        events = await _check_github(source, entities)
    else:
        events = await _check_mcp(platform, source, entities)
    await mark_checked(platform, [e["entity_id"] for e in entities])
    log.info("claude_code_recheck_done", platform=platform, checked=len(entities), resolved=len(events))
    return events


async def recheck(
    platform: str, source: dict[str, Any], after_hours: float | None = None
) -> list[LayaEvent]:
    """Re-check the due open cards of a platform; returns the closing events.

    `after_hours` overrides the source's `recheck_after_hours` (a manual sync
    re-checks cards sooner)."""
    if platform not in _RECHECKABLE:
        return []
    config = source.get("config") or {}
    if after_hours is None:
        after_hours = float(config.get("recheck_after_hours", 6))
    entities = await due_entities(platform, after_hours)
    return await check_entities(platform, source, entities)
