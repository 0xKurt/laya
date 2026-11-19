# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Gmail collector: one event per thread and sweep, plus one event per action
item of the user in Gemini meeting notes.

A thread splits at the user's latest mail like a Slack conversation: the
user's mail closes what came before, the mails after it form one event.
Meeting notes (from gemini-notes@google.com) become one `meeting` event per
action item, so each task is its own card that can be closed on its own;
the meeting's decisions and what the user waits on ride along as context.
"""

from __future__ import annotations

import html
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

PLATFORM = "gmail"
MEETING_NOTES_SENDER = "gemini-notes@google.com"

SCHEMA = {
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


def build_prompt(
    tools: dict[str, str], since: datetime, exclude: list[str], name: str, meeting_notes: bool,
) -> str:
    after = int(since.timestamp())
    excluded = " ".join(f"-from:{s}" for s in exclude)
    notes = ""
    if meeting_notes:
        drive = tools.get("drive_read")
        open_doc = (
            f"Open the notes document the mail links to with {drive} (the notes, not the "
            "transcript; fall back to the mail text if it cannot be read)."
            if drive else "Use the mail text."
        )
        notes = f"""
Mails from {MEETING_NOTES_SENDER} are meeting notes: set kind="meeting_notes". {open_doc}
Put into body: the meeting title on the first line; then "Action items for {name}:" with
every action item or next step assigned to {name} or to everyone, one per line starting
with "- ", with its due date if given; "Decisions:" with the decisions taken, one per line
starting with "- "; "Waiting on others:" with items {name} asked others for, one per line
starting with "- ". Leave a heading out when it has no lines. Use the document's link as
url when you opened it."""
    return f"""You are a data collector. Copy data exactly; do not judge relevance.
Use {tools['search']} with query "after:{after} -category:promotions -category:social {excluded}"
(pageSize 25, up to 2 pages). This includes mails I sent. For every thread found, call
{tools['get_thread']} and return each message dated after unix time {after}: message_id,
thread_id, date (ISO 8601), from_name, from_email, to, cc, subject, the plain-text body
(newest part only, without quoted history, at most 2000 characters), labels and the
message's web link as url. Set kind="email".{notes}
Return {{"messages": []}} if there is nothing."""


def meeting_sections(body: str) -> tuple[str, dict[str, list[str]]]:
    """Split a meeting-notes body into its title line and its `- ` sections."""
    lines = [line.rstrip() for line in body.splitlines() if line.strip()]
    title = lines[0].strip() if lines else ""
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith(("-", "*", "•")):
            current = stripped[:-1].strip().lower()
            sections[current] = []
        elif current and stripped.startswith(("-", "*", "•")):
            sections[current].append(stripped[1:].strip())
    return title, sections


def meeting_events(m: dict[str, Any]) -> list[LayaEvent]:
    """One event per action item of the user; none when the user has none."""
    title, sections = meeting_sections(m.get("body", ""))
    items = next((v for k, v in sections.items() if k.startswith("action items")), [])
    context = ""
    for name in ("decisions", "waiting on others"):
        if sections.get(name):
            context += f"\n\n{name.capitalize()} in that meeting:\n" + "\n".join(f"- {d}" for d in sections[name])
    events = []
    for i, item in enumerate(items):
        task = item.split(":", 1)[0] if ":" in item[:60] else item
        events.append(make_event(
            platform=PLATFORM,
            native_id=f"{m['message_id']}:item:{i}",
            timestamp=parse_ts(m["date"]),
            raw_event_type="meeting_action_item",
            actor_name="Meeting notes",
            actor_email=(m.get("from_email") or MEETING_NOTES_SENDER).lower(),
            actor_handle=None,
            subject_type="meeting",
            subject_id=f"{m['thread_id']}:item:{i}",
            subject_title=f"{task} ({title})" if title else task,
            subject_url=m.get("url"),
            body=f"Action item for me from the meeting {title}:\n{item}{context}",
            metadata={"gmail_thread_id": m["thread_id"], "meeting": title},
        ))
    return events


def _event(msgs: list[dict[str, Any]]) -> LayaEvent:
    first, last = msgs[0], msgs[-1]
    sender = (last.get("from_email") or "").lower()
    if len(msgs) == 1:
        body = last.get("body", "")
    else:
        body = "\n\n".join(
            f"From {m.get('from_name') or m.get('from_email')} ({m['date']}):\n{m.get('body', '')}"
            for m in msgs
        )
    return make_event(
        platform=PLATFORM,
        native_id=last["message_id"],
        timestamp=parse_ts(last["date"]),
        raw_event_type="new_email",
        actor_name=last.get("from_name") or "",
        actor_email=sender,
        actor_handle=None,
        subject_type="email_thread",
        subject_id=last["thread_id"],
        subject_title=last.get("subject") or "(no subject)",
        subject_url=first.get("url"),
        body=body,
        metadata={
            "gmail_thread_id": last["thread_id"],
            "gmail_labels": last.get("labels") or [],
            "gmail_from": sender,
            "gmail_to": last.get("to") or [],
            "gmail_cc": last.get("cc") or [],
            "gmail_message_count": len(msgs),
        },
    )


def build_events(messages: list[dict[str, Any]], mine: set[str]) -> list[LayaEvent]:
    messages = dedupe(messages, key=lambda m: m["message_id"])
    events = [e for m in messages if m.get("kind") == "meeting_notes" for e in meeting_events(m)]
    threads: dict[str, list[dict]] = {}
    for m in messages:
        if m.get("kind") != "meeting_notes":
            threads.setdefault(m["thread_id"], []).append(m)
    for msgs in threads.values():
        msgs.sort(key=lambda m: parse_ts(m["date"]))
        own, unanswered, _ = split_conversation(
            msgs, lambda m: (m.get("from_email") or "").lower() in mine
        )
        if own is not None:
            events.append(_event([own]))
        if unanswered:
            events.append(_event(unanswered))
    return events


async def collect(source: dict[str, Any], since: datetime) -> list[LayaEvent]:
    config = source.get("config") or {}
    prompt = build_prompt(
        source["tools"], since, list(config.get("exclude_senders") or []),
        my_name(source), bool(config.get("meeting_notes", True)),
    )
    out = await run_turn(source, PLATFORM, prompt, SCHEMA)
    messages = [
        # Mail bodies come back with HTML entities (`&quot;`) left in.
        {**m, "body": html.unescape(m.get("body") or ""), "subject": html.unescape(m.get("subject") or "")}
        for m in out.get("messages", []) if m.get("message_id") and m.get("thread_id")
    ]
    return build_events(messages, my_emails(source))
