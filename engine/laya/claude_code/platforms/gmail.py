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
from urllib.parse import quote

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

# Hard caps of one sweep.
SEARCH_PAGE_SIZE = 20  # threads per search page
SEARCH_PAGES = 2
BODY_CHARS = 1200  # characters kept of a mail body (newest part only)
READS_PER_SWEEP = 15  # mails from others whose body is read

# Compact field names keep the model's output short; `expand_message` maps
# them onto the names the rest of the module uses.
SCHEMA = {
    "type": "object",
    "properties": {
        "m": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "message id"},
                    "th": {"type": "string", "description": "thread id"},
                    "d": {"type": "string", "description": "date, ISO 8601"},
                    "fn": {"type": "string", "description": "sender name"},
                    "fe": {"type": "string", "description": "sender email"},
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "s": {"type": "string", "description": "subject"},
                    "b": {"type": "string", "description": "body"},
                    "k": {"type": "string", "enum": ["email", "meeting_notes"]},
                    "u": {"type": ["string", "null"], "description": "notes document link (meeting notes only)"},
                },
                "required": ["id", "th", "d", "fe", "s", "b", "k"],
            },
        },
    },
    "required": ["m"],
}


def build_prompt(
    tools: dict[str, str], since: datetime, exclude: list[str], name: str, meeting_notes: bool,
    me: str = "",
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
Mails from {MEETING_NOTES_SENDER} are meeting notes: set k="meeting_notes". {open_doc}
Put into b: the meeting title on the first line; then "Action items for {name}:" with
every action item or next step assigned to {name} or to everyone, one per line starting
with "- ", with its due date if given; "Decisions:" with the decisions taken, one per line
starting with "- "; "Waiting on others:" with items {name} asked others for, one per line
starting with "- ". Leave a heading out when it has no lines. Set u to the document's link
when you opened it."""
    read = tools.get("get_message")
    read_step = (
        f"call {read} (messageFormat PLAIN_TEXT) on that message" if read
        else f"call {tools['get_thread']} (messageFormat PLAIN_TEXT) once for its thread"
    )
    mine = f" (from {me})" if me else ""
    return f"""You are a data collector. Copy data exactly; do not judge relevance or summarize.
1. {tools['search']} with query "after:{after} -category:promotions -category:social {excluded}",
   pageSize {SEARCH_PAGE_SIZE}, view THREAD_VIEW_MINIMAL; follow pageToken at most {SEARCH_PAGES - 1} time.
2. Take each listed message dated after unix time {after}. Mine{mine}: b = its snippet, no
   further call. From someone else: {read_step}; make all these reads in parallel in ONE step
   (at most {READS_PER_SWEEP}, newest first) and set b = only the newest part of the plain-text
   body: no quoted history, no signature, at most {BODY_CHARS} characters.
Return per message in "m": id, th = thread id, d = date (ISO 8601), fn / fe = sender name /
email, to, cc, s = subject, b, k="email".{notes}
Call no other tool. Return {{"m": []}} if there is nothing."""


def thread_url(thread_id: str, account: str = "") -> str:
    """The Gmail web link of a thread (opened in the given account when known)."""
    auth = f"?authuser={quote(account)}" if account else ""
    return f"https://mail.google.com/mail/{auth}#all/{thread_id}"


def expand_message(m: dict[str, Any], account: str = "") -> dict[str, Any]:
    """A compact turn message in the module's own field names."""
    kind = m.get("k") or "email"
    url = (m.get("u") if kind == "meeting_notes" else None) or thread_url(m.get("th") or "", account)
    return {
        "message_id": m.get("id") or "",
        "thread_id": m.get("th") or "",
        "date": m.get("d") or "",
        "from_name": m.get("fn") or "",
        "from_email": m.get("fe") or "",
        "to": list(m.get("to") or []),
        "cc": list(m.get("cc") or []),
        # Mail text comes back with HTML entities (`&quot;`) left in.
        "subject": html.unescape(m.get("s") or ""),
        "body": html.unescape(m.get("b") or "")[: BODY_CHARS if kind == "email" else None],
        "url": url,
        "kind": kind,
    }


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
            # Replies are threaded by the newest message's id (create_draft).
            "gmail_message_id": last["message_id"],
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
    account = (source.get("identity") or {}).get("email") or ""
    prompt = build_prompt(
        source["tools"], since, list(config.get("exclude_senders") or []),
        my_name(source), bool(config.get("meeting_notes", True)), me=account,
    )
    out = await run_turn(source, PLATFORM, prompt, SCHEMA)
    messages = [expand_message(m, account) for m in out.get("m", []) if m.get("id") and m.get("th")]
    return build_events(messages, my_emails(source))
