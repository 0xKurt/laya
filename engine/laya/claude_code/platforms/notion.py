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

# Hard caps of one sweep.
OWN_PAGES = 10  # recently edited pages the user created
MENTION_PAGES = 10  # recently edited pages that name the user
COMMENT_READS = 12  # pages whose comments are read
TEXT_CHARS = 1500

# Compact field names keep the model's output short; `expand_item` maps them
# onto the names the rest of the module uses.
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "at": {"type": "string", "description": "created_at, ISO 8601"},
                    "k": {"type": "string", "enum": ["comment", "mention"]},
                    "p": {"type": "string", "description": "page id"},
                    "pt": {"type": "string", "description": "page title"},
                    "d": {"type": ["string", "null"], "description": "discussion id"},
                    "an": {"type": "string", "description": "author name"},
                    "ae": {"type": "string", "description": "author email"},
                    "t": {"type": "string", "description": "text"},
                },
                "required": ["id", "at", "k", "p", "pt", "an", "t"],
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
Make the two searches in parallel in ONE step, then all comment reads in parallel in ONE step.
1. {tools['search']} with query "", filters {{"created_by_user_ids": ["me"],
   "last_edited_date_range": {{"start_date": "{day}"}}}}, sort "last_edited",
   page_size {OWN_PAGES}, max_highlight_length 0.
2. {tools['search']} with query "{name}", filters {{"last_edited_date_range":
   {{"start_date": "{day}"}}}}, sort "last_edited", page_size {MENTION_PAGES}, max_highlight_length 300.
3. Call {tools['comments']} for the pages found, most recently edited first, at most
   {COMMENT_READS} pages, and return each comment created after {iso}: k="comment",
   id = the comment id, d = discussion id, an / ae = author name / email, t = text,
   at = created time, p = page id, pt = page title.
4. For pages from step 2 edited after {iso} whose highlight @-mentions me, return one item
   k="mention" with id = page id + ":" + the page's last edited time, at = that time,
   an = who last edited it, t = the highlight. Do not fetch pages.
Texts at most {TEXT_CHARS} characters. Call no other tool.
Return {{"items": []}} if there is nothing."""


def page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def expand_item(it: dict[str, Any]) -> dict[str, Any]:
    """A compact turn item in the module's own field names."""
    page = it.get("p") or ""
    return {
        "id": it.get("id") or "",
        "created_at": it.get("at") or "",
        "kind": it.get("k") or "comment",
        "page_id": page,
        "page_title": it.get("pt") or "",
        "page_url": page_url(page) if page else None,
        "discussion_id": it.get("d") or None,
        "author_name": it.get("an") or "",
        "author_email": it.get("ae") or "",
        "text": (it.get("t") or "")[:TEXT_CHARS],
    }


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
    items = [expand_item(it) for it in out.get("items", []) if it.get("id") and it.get("at")]
    return build_events(items, my_emails(source))
