# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Notion collector: comments on the user's pages and on pages that mention
the user, and @-mentions of the user in page content.

A conversation is a page's discussion (`<page_id>:<discussion_id>`) or the
page itself (mentions, comments without a discussion). It splits at the
user's latest comment like a Slack conversation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from laya.claude_code.platforms.common import (
    dedupe,
    make_event,
    my_emails,
    my_name,
    parse_ts,
    run_turn,
    split_conversation,
)
from laya.models.event import LayaEvent

PLATFORM = "notion"

SCHEMA = {
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


def build_prompt(tools: dict[str, str], since: datetime, name: str) -> str:
    day = since.strftime("%Y-%m-%d")
    iso = since.isoformat()
    who = f" (call {tools['self']} for my user id and email)" if tools.get("self") else ""
    return f"""You are a data collector. Copy data exactly; do not summarize or judge.
I am {name}{who}.
1. {tools['search']} with query "", filters {{"created_by_user_ids": ["me"],
   "last_edited_date_range": {{"start_date": "{day}"}}}}, sort "last_edited", page_size 25.
2. {tools['search']} with query "{name}", filters {{"last_edited_date_range":
   {{"start_date": "{day}"}}}}, sort "last_edited", page_size 15.
For every page found, call {tools['comments']} and return each comment created after
{iso}: kind="comment", id = the comment id, discussion_id, author_name, author_email,
text, created_at, page_id, page_title, page_url.
For pages from step 2 that @-mention me in their content (fetch them with
{tools['fetch']}), return one item kind="mention" with id = page id + ":" + the page's
last edited time, created_at = that time, author = who last edited it, and text = the
paragraph that mentions me.
Return {{"items": []}} if there is nothing."""


def conversation_of(item: dict[str, Any]) -> str:
    if item.get("discussion_id"):
        return f"{item['page_id']}:{item['discussion_id']}"
    return item["page_id"]


def _event(items: list[dict[str, Any]]) -> LayaEvent:
    last = items[-1]
    if len(items) == 1:
        body = last.get("text") or ""
    else:
        body = "\n\n".join(f"{it.get('author_name') or 'Someone'}: {it.get('text') or ''}" for it in items)
    return make_event(
        platform=PLATFORM,
        native_id=last["id"],
        timestamp=parse_ts(last["created_at"]),
        raw_event_type=f"page_{last.get('kind') or 'comment'}",
        actor_name=last.get("author_name") or "",
        actor_email=last.get("author_email") or "",
        actor_handle=None,
        subject_type="page",
        subject_id=conversation_of(last),
        subject_title=last.get("page_title") or "Notion page",
        subject_url=last.get("page_url"),
        body=body,
        metadata={"notion_page_id": last["page_id"], "notion_discussion_id": last.get("discussion_id")},
    )


def build_events(items: list[dict[str, Any]], mine: set[str]) -> list[LayaEvent]:
    items = dedupe([it for it in items if it.get("page_id")], key=lambda it: it["id"])
    conversations: dict[str, list[dict]] = {}
    for it in items:
        conversations.setdefault(conversation_of(it), []).append(it)
    events: list[LayaEvent] = []
    for msgs in conversations.values():
        msgs.sort(key=lambda it: parse_ts(it["created_at"]))
        own, unanswered, _ = split_conversation(
            msgs, lambda it: (it.get("author_email") or "").lower() in mine
        )
        if own is not None:
            events.append(_event([own]))
        if unanswered:
            events.append(_event(unanswered))
    return events


async def collect(source: dict[str, Any], since: datetime) -> list[LayaEvent]:
    out = await run_turn(
        source, PLATFORM, build_prompt(source["tools"], since, my_name(source)), SCHEMA
    )
    return build_events(out.get("items", []), my_emails(source))
