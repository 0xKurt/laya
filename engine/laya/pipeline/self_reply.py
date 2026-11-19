# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Close cards the user already answered with their own message.

When the actor of an event is the user (the team.json member with role
`self`), the event is the user's own reply, comment, email or merge. Every
card that is still waiting on the user in the same conversation, and whose
event is older than the reply, is closed as `done`. No LLM is involved, and
the user's own event produces no card.

A conversation is identified per platform:
- slack: channel + thread root ts; in a direct message, the channel alone, so
  any later message of the user in that DM answers every earlier ask there;
- gmail: gmail_thread_id;
- every other platform: subject.id (Linear issue identifier, GitHub repo#number).

Laya never closes a card the user reopened (`status_set_by = 'user'`).
"""

from __future__ import annotations

import json

import structlog

from laya.db.timeutil import db_ts
from laya.models.card_lifecycle import transition_card_status
from laya.models.event import LayaEvent
from laya.pipeline.card_status import SET_BY_LAYA, SET_BY_USER

log = structlog.get_logger()

# Statuses of a card that is waiting on the user. A card that runs an agent,
# executes an action or waits for agent input is left alone: the user's message
# on the platform does not settle that work.
_ANSWERABLE_STATUSES = ("ready", "requires_approval")

SELF_REPLY_REASON = "You replied in this conversation"

# filter_rule broadcast for the user's own event, which is marked filtered.
SELF_EVENT_FILTER = "Your own message"


def is_self_event(event: LayaEvent, self_user: dict | None) -> bool:
    """True when the event's actor is the user running Laya.

    Matches the actor's email against all of the user's emails and the actor's
    platform handle against the user's accounts, both case-insensitive. A
    collector that already knows the message is the user's own sets
    `content.metadata.is_self` to true.
    """
    if event.content.metadata.get("is_self") is True:
        return True
    if not self_user:
        return False
    email = (event.actor.email or "").strip().lower()
    if email and email in {e.strip().lower() for e in self_user.get("emails", [])}:
        return True
    handle = (event.actor.platform_handle or "").strip().lower()
    if handle and handle in {a.strip().lower() for a in self_user.get("accounts", [])}:
        return True
    return False


def conversation_key(platform: str, subject_id: str, metadata: dict) -> tuple | None:
    """The key that identifies the conversation an event belongs to.

    Returns None when the event carries nothing to identify it; such an event
    never closes a card.
    """
    if platform == "slack":
        channel = metadata.get("slack_channel") or metadata.get("slack_channel_id")
        if channel:
            if metadata.get("slack_channel_type") == "dm" or str(channel).startswith("D"):
                return ("slack", channel)
            thread_ts = metadata.get("slack_thread_ts")
            if thread_ts:
                return ("slack", channel, thread_ts)
    elif platform == "gmail":
        thread_id = metadata.get("gmail_thread_id")
        if thread_id:
            return ("gmail", thread_id)
    if subject_id:
        return (platform, subject_id)
    return None


async def close_answered_cards(db, event: LayaEvent) -> list[dict]:
    """Set to `done` the open cards of earlier events in the same conversation.

    Returns the closed cards as dicts with `card_id` and `header`.
    """
    key = conversation_key(event.source.platform, event.subject.id, event.content.metadata)
    if key is None:
        return []

    placeholders = ", ".join("?" for _ in _ANSWERABLE_STATUSES)
    rows = await db.execute_fetchall(
        f"""SELECT c.card_id, c.header, e.subject_id, e.content_metadata
            FROM action_cards c JOIN events e ON e.event_id = c.event_id
            WHERE e.source_platform = ? AND e.event_id != ?
              AND e.timestamp < ?
              AND c.status IN ({placeholders})
              AND (c.status_set_by IS NULL OR c.status_set_by != ?)""",
        (event.source.platform, event.event_id, db_ts(event.timestamp),
         *_ANSWERABLE_STATUSES, SET_BY_USER),
    )

    reason = SELF_REPLY_REASON
    if event.subject.url:
        reason = f"{reason}: {event.subject.url}"

    closed: list[dict] = []
    for row in rows:
        try:
            metadata = json.loads(row["content_metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if conversation_key(event.source.platform, row["subject_id"], metadata) != key:
            continue
        try:
            await transition_card_status(
                row["card_id"],
                "done",
                actor="pipeline",
                extra_fields={"status_reason": reason, "status_set_by": SET_BY_LAYA},
            )
        except ValueError:
            # The card's status changed between the SELECT and the write, for
            # example the user dismissed it. Leave it as the user set it.
            continue
        closed.append({"card_id": row["card_id"], "header": row["header"]})

    if closed:
        log.info(
            "self_reply_closed_cards",
            event_id=event.event_id,
            platform=event.source.platform,
            closed_count=len(closed),
        )
    return closed
