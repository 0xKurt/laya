# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for pipeline/self_reply.py: the user's own message closes the open
cards of its conversation, without an LLM call."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from laya.db.timeutil import db_ts
from laya.models.event import LayaEvent
from laya.pipeline.self_reply import (
    SELF_REPLY_REASON,
    close_answered_cards,
    conversation_key,
    is_self_event,
)

SELF_USER = {
    "name": "Kim",
    "email": "kim@company.com",
    "emails": ["kim@company.com", "Kim.Alias@Personal.com"],
    "accounts": ["U_KIM", "kim-gh"],
}

SELF_TEAM = {
    "members": [
        {"name": "Kim", "email": "kim@company.com", "role": "self",
         "aliases": ["kim.alias@personal.com"], "accounts": ["U_KIM"]},
        {"name": "Mike", "email": "mike@company.com", "role": "manager"},
    ]
}


def _event(event_id, *, platform="slack", hour=10, email="mike@company.com", handle=None,
           subject_id="thread-C1-100.0", metadata=None, url=None,
           raw_event_type="message_received") -> LayaEvent:
    return LayaEvent(
        event_id=event_id,
        timestamp=datetime(2026, 2, 22, hour, 0, 0, tzinfo=timezone.utc),
        source={"platform": platform, "raw_event_type": raw_event_type},
        actor={"name": "Someone", "email": email, "platform_handle": handle},
        subject={"type": "thread", "id": subject_id, "title": "t", "url": url},
        content={"body": "b", "metadata": metadata or {}},
    )


async def _store(db, event: LayaEvent) -> None:
    await db.execute(
        """INSERT INTO events
           (event_id, timestamp, source_platform, source_raw_event_type,
            actor_name, actor_email, actor_handle, subject_type, subject_id,
            subject_title, subject_url, content_body, content_metadata, raw_json,
            processing_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
        (event.event_id, db_ts(event.timestamp), event.source.platform,
         event.source.raw_event_type, event.actor.name, event.actor.email,
         event.actor.platform_handle, event.subject.type, event.subject.id,
         event.subject.title, event.subject.url, event.content.body,
         json.dumps(event.content.metadata), event.model_dump_json()),
    )
    await db.commit()


async def _ask(db, event: LayaEvent, card_id: str, status="ready", set_by=None) -> None:
    """Store `event` and an open card for it."""
    await _store(db, event)
    ts = db_ts(event.timestamp)
    await db.execute(
        """INSERT INTO action_cards
           (card_id, event_id, priority, persona, category, header, summary, status,
            status_set_by, entity_id, created_at, group_active_at)
           VALUES (?, ?, 'HIGH', 'COMMS', 'COMMUNICATION', ?, 's', ?, ?, ?, ?, ?)""",
        (card_id, event.event_id, card_id, status, set_by, event.entity_id, ts, ts),
    )
    await db.commit()


async def _close(db, event: LayaEvent):
    await _store(db, event)
    with patch("laya.models.card_lifecycle.manager.broadcast", new_callable=AsyncMock) as bc:
        closed = await close_answered_cards(db, event)
    return closed, bc


async def _card(db, card_id):
    rows = await db.execute_fetchall(
        "SELECT status, status_set_by, status_reason FROM action_cards WHERE card_id = ?",
        (card_id,),
    )
    return dict(rows[0])


# ---------------------------------------------------------------------------
# Self detection
# ---------------------------------------------------------------------------


def test_is_self_by_email_alias_case_insensitive():
    assert is_self_event(_event("e", email="KIM.alias@personal.com"), SELF_USER)


def test_is_self_by_platform_handle():
    assert is_self_event(_event("e", email="", handle="u_kim"), SELF_USER)


def test_non_self_actor():
    assert not is_self_event(_event("e"), SELF_USER)
    assert not is_self_event(_event("e"), None)


def test_is_self_metadata_flag():
    assert is_self_event(_event("e", metadata={"is_self": True}), None)
    assert not is_self_event(_event("e", metadata={"is_self": "yes"}), None)


def test_conversation_keys():
    assert conversation_key("slack", "x", {"slack_channel": "C1", "slack_thread_ts": "1.0"}) == ("slack", "C1", "1.0")
    dm = {"slack_channel": "D9", "slack_channel_type": "dm", "slack_thread_ts": "5.0"}
    assert conversation_key("slack", "x", dm) == ("slack", "D9")
    assert conversation_key("gmail", "x", {"gmail_thread_id": "t1"}) == ("gmail", "t1")
    assert conversation_key("linear", "ENG-1", {}) == ("linear", "ENG-1")
    assert conversation_key("jira", "", {}) is None


# ---------------------------------------------------------------------------
# Closing cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_thread_reply_closes_ask(db):
    meta = {"slack_channel": "C1", "slack_thread_ts": "100.0", "slack_channel_type": "channel"}
    await _ask(db, _event("evt_ask", hour=9, metadata=meta), "card_ask")
    # Another thread in the same channel stays open.
    other = {**meta, "slack_thread_ts": "200.0"}
    await _ask(db, _event("evt_other", hour=9, subject_id="thread-C1-200.0", metadata=other), "card_other")

    url = "https://x.slack.com/archives/C1/p300"
    closed, bc = await _close(db, _event("evt_me", hour=10, email="kim@company.com", metadata=meta, url=url))

    assert [c["card_id"] for c in closed] == ["card_ask"]
    card = await _card(db, "card_ask")
    assert card["status"] == "done"
    assert card["status_set_by"] == "laya"
    assert card["status_reason"] == f"{SELF_REPLY_REASON}: {url}"
    assert (await _card(db, "card_other"))["status"] == "ready"
    assert bc.await_args.args[0]["type"] == "card_updated"


@pytest.mark.asyncio
async def test_slack_dm_reply_without_thread_closes_ask(db):
    ask_meta = {"slack_channel": "D1", "slack_channel_type": "dm", "slack_thread_ts": "100.0"}
    await _ask(db, _event("evt_ask", hour=9, subject_id="thread-D1-100.0", metadata=ask_meta), "card_ask")

    reply_meta = {"slack_channel": "D1", "slack_channel_type": "dm", "slack_thread_ts": "300.0"}
    closed, _ = await _close(db, _event(
        "evt_me", hour=10, email="", handle="U_KIM", subject_id="thread-D1-300.0", metadata=reply_meta,
    ))

    assert [c["card_id"] for c in closed] == ["card_ask"]


@pytest.mark.asyncio
async def test_gmail_thread_reply_closes_ask(db):
    await _ask(db, _event("evt_ask", platform="gmail", hour=9, subject_id="m1",
                          metadata={"gmail_thread_id": "t1"}), "card_ask")
    await _ask(db, _event("evt_other", platform="gmail", hour=9, subject_id="m2",
                          metadata={"gmail_thread_id": "t2"}), "card_other")

    closed, _ = await _close(db, _event("evt_me", platform="gmail", hour=10, email="kim@company.com",
                                        subject_id="m3", metadata={"gmail_thread_id": "t1"}))

    assert [c["card_id"] for c in closed] == ["card_ask"]
    assert (await _card(db, "card_other"))["status"] == "ready"


@pytest.mark.asyncio
async def test_linear_comment_closes_ask(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=9, subject_id="ENG-12"), "card_ask",
               status="requires_approval")

    closed, _ = await _close(db, _event("evt_me", platform="linear", hour=10,
                                        email="kim@company.com", subject_id="ENG-12"))

    assert [c["card_id"] for c in closed] == ["card_ask"]


@pytest.mark.asyncio
async def test_github_pr_merged_by_self_closes_cards(db):
    await _ask(db, _event("evt_review", platform="github", hour=9, subject_id="org/repo#7",
                          raw_event_type="pr_review_requested"), "card_review")

    closed, _ = await _close(db, _event("evt_merge", platform="github", hour=10, email="",
                                        handle="kim-gh", subject_id="org/repo#7",
                                        raw_event_type="pr_merged"))

    assert [c["card_id"] for c in closed] == ["card_review"]


@pytest.mark.asyncio
async def test_older_self_message_does_not_close_newer_card(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=11, subject_id="ENG-12"), "card_ask")

    closed, _ = await _close(db, _event("evt_me", platform="linear", hour=10,
                                        email="kim@company.com", subject_id="ENG-12"))

    assert closed == []
    assert (await _card(db, "card_ask"))["status"] == "ready"


@pytest.mark.asyncio
async def test_user_reopened_card_stays_open(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=9, subject_id="ENG-12"), "card_ask",
               set_by="user")

    closed, _ = await _close(db, _event("evt_me", platform="linear", hour=10,
                                        email="kim@company.com", subject_id="ENG-12"))

    assert closed == []
    assert (await _card(db, "card_ask"))["status"] == "ready"


@pytest.mark.asyncio
async def test_card_running_agent_is_left_alone(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=9, subject_id="ENG-12"), "card_ask",
               status="agent_running")

    closed, _ = await _close(db, _event("evt_me", platform="linear", hour=10,
                                        email="kim@company.com", subject_id="ENG-12"))

    assert closed == []


# ---------------------------------------------------------------------------
# process_event
# ---------------------------------------------------------------------------


async def _process(db, event, team):
    from laya.pipeline import queue

    with patch("laya.config.load_team", return_value=team), \
         patch("laya.pipeline.ingest.load_team", return_value=team), \
         patch("laya.pipeline.router.run_router", new_callable=AsyncMock, return_value=None) as router, \
         patch("laya.api.websocket.manager.broadcast", new_callable=AsyncMock):
        await queue.process_event(event.event_id)
    rows = await db.execute_fetchall(
        "SELECT processing_status FROM events WHERE event_id = ?", (event.event_id,)
    )
    return rows[0]["processing_status"], router


@pytest.mark.asyncio
async def test_process_event_self_closes_card_without_llm(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=9, subject_id="ENG-12"), "card_ask")
    me = _event("evt_me", platform="linear", hour=10, email="kim@company.com", subject_id="ENG-12")
    await _store(db, me)

    status, router = await _process(db, me, SELF_TEAM)

    assert status == "filtered"
    router.assert_not_called()
    assert (await _card(db, "card_ask"))["status"] == "done"
    cards = await db.execute_fetchall("SELECT 1 FROM action_cards WHERE event_id = 'evt_me'")
    assert cards == []


@pytest.mark.asyncio
async def test_process_event_is_self_flag_without_team(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=9, subject_id="ENG-12"), "card_ask")
    me = _event("evt_me", platform="linear", hour=10, subject_id="ENG-12", metadata={"is_self": True})
    await _store(db, me)

    status, router = await _process(db, me, {"members": []})

    assert status == "filtered"
    router.assert_not_called()
    assert (await _card(db, "card_ask"))["status"] == "done"


@pytest.mark.asyncio
async def test_process_event_non_self_untouched(db):
    await _ask(db, _event("evt_ask", platform="linear", hour=9, subject_id="ENG-12"), "card_ask")
    other = _event("evt_other", platform="linear", hour=10, subject_id="ENG-12")
    await _store(db, other)

    # The router mock returns no output, so the event completes without a card.
    status, router = await _process(db, other, SELF_TEAM)

    router.assert_awaited_once()
    assert status == "completed"
    assert (await _card(db, "card_ask"))["status"] == "ready"
