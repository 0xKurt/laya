# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Linear collector: notifications and assigned issues, one event per issue
and sweep.

- An issue whose current status is Done / Canceled / Duplicate yields one
  closing event (`resolved_in_source`), which concludes the issue's cards.
- Review requests are emitted as `review_requested`; the seeded rule
  "Claude Code · Linear: ignore review requests" drops them.
- Otherwise an issue splits at the user's latest comment like a conversation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from laya.claude_code.platforms.common import (
    dedupe,
    make_event,
    my_emails,
    parse_ts,
    run_turn,
    split_conversation,
)
from laya.models.event import LayaEvent

PLATFORM = "linear"

RESOLVED_STATUSES = {"done", "canceled", "cancelled", "duplicate", "completed"}
_REVIEW_REQUEST = re.compile(r"request(?:ed|s)?\b.*\breview", re.IGNORECASE)

KINDS = ["comment", "mention", "assigned", "status_changed", "issue_created", "review_requested", "other"]

# Hard caps of one sweep.
ISSUES_LIMIT = 25  # assigned issues listed
TEXT_CHARS = 1500  # characters kept of a comment
DESCRIPTION_CHARS = 500  # characters kept of an issue description

# Compact field names keep the model's output short; `expand_item` maps them
# onto the names the rest of the module uses.
SCHEMA = {
    "type": "object",
    "properties": {
        "ws": {"type": ["string", "null"], "description": "workspace slug of the issue links"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "at": {"type": "string", "description": "created_at, ISO 8601"},
                    "k": {"type": "string", "enum": KINDS},
                    "i": {"type": "string", "description": "issue identifier"},
                    "it": {"type": "string", "description": "issue title"},
                    "st": {"type": "string", "description": "issue status"},
                    "an": {"type": "string", "description": "actor name"},
                    "ae": {"type": "string", "description": "actor email"},
                    "t": {"type": "string", "description": "text"},
                },
                "required": ["id", "at", "k", "i", "it", "t"],
            },
        },
    },
    "required": ["items"],
}


def build_prompt(tools: dict[str, str], since: datetime) -> str:
    iso = since.isoformat()
    return f"""You are a data collector. Copy data exactly; do not judge relevance.
Make the two calls below in parallel in ONE step, then answer.
1. Call {tools['notifications']} and take every notification created after {iso} (read or
   unread): mentions, comments, assignments, status changes, review requests. For each
   return id (the notification id), at = created time, k (review_requested when someone
   requested a review), i = issue identifier, it = issue title, st = the issue's current
   status name, an / ae = actor name / email and t: the comment body (at most {TEXT_CHARS}
   characters) for comments and mentions, otherwise one sentence describing the change.
2. Call {tools['list_issues']} with assignee="me", updatedAt="{iso}", limit={ISSUES_LIMIT} and add
   each issue not already covered by step 1 with k="assigned" (id = "issue:" + identifier +
   ":" + its updatedAt, at = its updatedAt, st = its status, t = its status and the first
   {DESCRIPTION_CHARS} characters of its description).
ws = the workspace slug of the issue links (linear.app/<ws>/issue/...).
Call no other tool. Return {{"items": []}} if there is nothing."""


def issue_url(workspace: str | None, identifier: str) -> str | None:
    return f"https://linear.app/{workspace}/issue/{identifier}" if workspace and identifier else None


def expand_item(it: dict[str, Any], workspace: str | None) -> dict[str, Any]:
    """A compact turn item in the module's own field names."""
    identifier = it.get("i") or ""
    return {
        "id": it.get("id") or "",
        "created_at": it.get("at") or "",
        "kind": it.get("k") or "other",
        "issue_identifier": identifier,
        "issue_title": it.get("it") or "",
        "issue_url": issue_url(workspace, identifier),
        "issue_status": it.get("st") or "",
        "actor_name": it.get("an") or "",
        "actor_email": it.get("ae") or "",
        "text": (it.get("t") or "")[:TEXT_CHARS],
    }


def is_review_request(item: dict[str, Any]) -> bool:
    return item.get("kind") == "review_requested" or bool(_REVIEW_REQUEST.search(item.get("text") or ""))


def is_resolved(item: dict[str, Any]) -> bool:
    return (item.get("issue_status") or "").strip().lower() in RESOLVED_STATUSES


def _event(items: list[dict[str, Any]], *, raw: str | None = None, resolved: bool = False) -> LayaEvent:
    last = items[-1]
    if len(items) == 1:
        body = last.get("text") or ""
    else:
        body = "\n\n".join(f"{it.get('actor_name') or 'Someone'}: {it.get('text') or ''}" for it in items)
    metadata: dict[str, Any] = {"linear_status": last.get("issue_status")}
    if resolved:
        metadata["resolved_in_source"] = True
    # A resolution gets its own id: the notification it rides on may already
    # have been delivered while the issue was open.
    native_id = f"resolved:{last['id']}" if resolved else last["id"]
    return make_event(
        platform=PLATFORM,
        native_id=native_id,
        timestamp=parse_ts(last["created_at"]),
        raw_event_type=raw or f"issue_{last.get('kind') or 'other'}",
        actor_name=last.get("actor_name") or "",
        actor_email=last.get("actor_email") or "",
        actor_handle=None,
        subject_type="ticket",
        subject_id=last["issue_identifier"],
        subject_title=f"{last['issue_identifier']}: {last.get('issue_title') or ''}",
        subject_url=last.get("issue_url"),
        body=body,
        metadata=metadata,
    )


def build_events(items: list[dict[str, Any]], mine: set[str]) -> list[LayaEvent]:
    items = dedupe([it for it in items if it.get("issue_identifier")], key=lambda it: it["id"])
    events: list[LayaEvent] = []
    issues: dict[str, list[dict]] = {}
    for it in items:
        if is_review_request(it):
            events.append(_event([it], raw="review_requested"))
        else:
            issues.setdefault(it["issue_identifier"], []).append(it)
    for msgs in issues.values():
        msgs.sort(key=lambda it: parse_ts(it["created_at"]))
        if any(is_resolved(it) for it in msgs):
            # The issue is finished: nothing more is asked of the user.
            events.append(_event([msgs[-1]], raw="issue_status_changed", resolved=True))
            continue
        own, unanswered, _ = split_conversation(
            msgs, lambda it: (it.get("actor_email") or "").lower() in mine
        )
        if own is not None:
            events.append(_event([own]))
        if unanswered:
            events.append(_event(unanswered))
    return events


async def collect(source: dict[str, Any], since: datetime) -> list[LayaEvent]:
    out = await run_turn(source, PLATFORM, build_prompt(source["tools"], since), SCHEMA)
    workspace = out.get("ws") or None
    items = [expand_item(it, workspace) for it in out.get("items", []) if it.get("id") and it.get("at")]
    return build_events(items, my_emails(source))
