# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for conclusions: the user's own message, or an item resolved in its
source, closes the open cards of its entity (pipeline/card_status.apply_conclusion
and the process_event hook in pipeline/queue.py)."""

import json
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from laya.db.timeutil import db_ts
from laya.models.event import LayaEvent
from laya.pipeline.card_status import (
    RESOLVED_IN_SOURCE_REASON,
    SELF_REPLY_REASON,
    apply_conclusion,
    decide_card_status,
    entity_status_lock,
    is_conclusion,
)

TEAM = {
    "members": [
        {"name": "Kim", "email": "kim@company.com", "role": "self",
         "aliases": ["kim@personal.com"], "accounts": ["U_KIM", "kim-gh"]},
        {"name": "Mike", "email": "mike@company.com", "role": "manager"},
    ]
}


def _event(event_id, *, platform="slack", subject_type="thread", subject_id="C1:100.0",
           hour=10, email="mike@company.com", handle=None, name="Mike",
           url=None, metadata=None, raw="message_received") -> LayaEvent:
    return LayaEvent(
        event_id=event_id,
        timestamp=datetime(2026, 2, 22, hour, 0, 0, tzinfo=timezone.utc),
        source={"platform": platform, "raw_event_type": raw,
                "connection_id": f"claude_code:{platform}"},
        actor={"name": name, "email": email, "platform_handle": handle},
        subject={"type": subject_type, "id": subject_id, "title": "t", "url": url},
        content={"body": "b", "metadata": {"claude_code": True, **(metadata or {})}},
    )


async def _store(db, event: LayaEvent, status="queued") -> None:
    await db.execute(
        """INSERT INTO events
           (event_id, timestamp, source_platform, source_connection_id, source_raw_event_type,
            actor_name, actor_email, actor_handle, subject_type, subject_id,
            subject_title, subject_url, content_body, content_metadata, raw_json,
            processing_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event.event_id, db_ts(event.timestamp), event.source.platform,
         event.source.connection_id, event.source.raw_event_type, event.actor.name,
         event.actor.email, event.actor.platform_handle, event.subject.type, event.subject.id,
         event.subject.title, event.subject.url, event.content.body,
         json.dumps(event.content.metadata), event.model_dump_json(), status),
    )
    await db.commit()


async def _card_for(db, event: LayaEvent, card_id: str, status="ready", set_by=None) -> None:
    await _store(db, event, status="completed")
    ts = db_ts(event.timestamp)
    await db.execute(
        """INSERT INTO action_cards
           (card_id, event_id, priority, persona, category, header, summary, status,
            status_set_by, entity_id, created_at, group_active_at)
           VALUES (?, ?, 'HIGH', 'COMMS', 'COMMUNICATION', ?, 's', ?, ?, ?, ?, ?)""",
        (card_id, event.event_id, card_id, status, set_by, event.entity_id, ts, ts),
    )
    await db.commit()


async def _card(db, card_id):
    rows = await db.execute_fetchall(
        "SELECT status, status_set_by, status_reason FROM action_cards WHERE card_id = ?",
        (card_id,),
    )
    return dict(rows[0])


async def _conclude(db, event, relationship="self"):
    await _store(db, event)
    with patch("laya.models.card_lifecycle.manager.broadcast", new_callable=AsyncMock), \
         patch("laya.pipeline.summarize.trigger_summary_status_update", new_callable=AsyncMock):
        return await apply_conclusion(event, relationship)


def test_is_conclusion():
    assert is_conclusion(_event("e"), "self")
    assert not is_conclusion(_event("e"), "manager")
    assert is_conclusion(_event("e", metadata={"resolved_in_source": True}), "external")
    assert not is_conclusion(_event("e", metadata={"resolved_in_source": "yes"}), "external")


@pytest.mark.asyncio
async def test_self_reply_closes_ready_and_requires_approval_cards(db):
    await _card_for(db, _event("evt_a", hour=8), "card_a")
    await _card_for(db, _event("evt_b", hour=9), "card_b", status="requires_approval")
    await _card_for(db, _event("evt_other", hour=9, subject_id="C1:200.0"), "card_other")
    await _card_for(db, _event("evt_agent", hour=9), "card_agent", status="agent_running")

    url = "https://x.slack.com/archives/C1/p300"
    closed = await _conclude(db, _event("evt_me", hour=10, email="", handle="U_KIM", url=url))

    assert sorted(c["card_id"] for c in closed) == ["card_a", "card_b"]
    card = await _card(db, "card_a")
    assert card == {"status": "done", "status_set_by": "laya",
                    "status_reason": f"{SELF_REPLY_REASON}: {url}"}
    assert (await _card(db, "card_other"))["status"] == "ready"
    assert (await _card(db, "card_agent"))["status"] == "agent_running"
    rows = await db.execute_fetchall("SELECT filter_rule FROM events WHERE event_id = 'evt_me'")
    assert rows[0]["filter_rule"] == f"{SELF_REPLY_REASON}: {url}"


@pytest.mark.asyncio
async def test_conclusion_leaves_later_and_user_reopened_cards(db):
    await _card_for(db, _event("evt_later", hour=11), "card_later")
    await _card_for(db, _event("evt_reopened", hour=8), "card_reopened", set_by="user")

    closed = await _conclude(db, _event("evt_me", hour=10, handle="U_KIM"))

    assert closed == []
    assert (await _card(db, "card_later"))["status"] == "ready"
    assert (await _card(db, "card_reopened"))["status"] == "ready"


@pytest.mark.asyncio
async def test_resolved_in_source_reason(db):
    kw = dict(platform="linear", subject_type="ticket", subject_id="TRAD-12")
    await _card_for(db, _event("evt_ask", hour=8, **kw), "card_ask")
    closed = await _conclude(
        db,
        _event("evt_done", hour=10, url="https://linear.app/x/TRAD-12",
               metadata={"resolved_in_source": True}, **kw),
        relationship="external",
    )
    assert [c["card_id"] for c in closed] == ["card_ask"]
    assert (await _card(db, "card_ask"))["status_reason"].startswith(RESOLVED_IN_SOURCE_REASON)


@pytest.mark.asyncio
async def test_race_reply_processed_before_the_card(db):
    """The user's reply is processed first; the card for the earlier message is
    created afterwards and must be created done."""
    await _conclude(db, _event("evt_me", hour=10, handle="U_KIM", url="https://s/p1"))

    ask = _event("evt_ask", hour=9)
    await _store(db, ask)
    async with entity_status_lock(ask.entity_id):
        decision = await decide_card_status(
            db, entity_id=ask.entity_id, event_id=ask.event_id,
            event_ts=db_ts(ask.timestamp), base_status="ready", verdict=None,
        )
    assert decision.status == "done"
    assert decision.status_set_by == "laya"
    assert decision.status_reason == f"{SELF_REPLY_REASON}: https://s/p1"

    # requires_approval cards are closed the same way.
    async with entity_status_lock(ask.entity_id):
        decision = await decide_card_status(
            db, entity_id=ask.entity_id, event_id=ask.event_id,
            event_ts=db_ts(ask.timestamp), base_status="requires_approval", verdict=None,
        )
    assert decision.status == "done"

    # A later message opens a new card: the reply does not conclude the future.
    later = _event("evt_new", hour=11)
    await _store(db, later)
    decision = await decide_card_status(
        db, entity_id=later.entity_id, event_id=later.event_id,
        event_ts=db_ts(later.timestamp), base_status="ready", verdict=None,
    )
    assert decision.status == "ready"

    # Another entity is unaffected.
    other = _event("evt_other", hour=9, subject_id="C1:999.0")
    await _store(db, other)
    decision = await decide_card_status(
        db, entity_id=other.entity_id, event_id=other.event_id,
        event_ts=db_ts(other.timestamp), base_status="ready", verdict=None,
    )
    assert decision.status == "ready"


@pytest.mark.asyncio
async def test_entity_with_colons_in_subject_id(db):
    kw = dict(platform="notion", subject_type="page", subject_id="pg1:disc:2")
    await _conclude(db, _event("evt_me", hour=10, email="kim@company.com", **kw))
    ask = _event("evt_ask", hour=9, **kw)
    await _store(db, ask)
    decision = await decide_card_status(
        db, entity_id=ask.entity_id, event_id=ask.event_id,
        event_ts=db_ts(ask.timestamp), base_status="ready", verdict=None,
    )
    assert decision.status == "done"


# ---------------------------------------------------------------------------
# process_event hook
# ---------------------------------------------------------------------------


async def _process(db, event):
    from laya.pipeline import queue

    with ExitStack() as stack:
        stack.enter_context(patch("laya.config.load_team", return_value=TEAM))
        stack.enter_context(patch("laya.pipeline.ingest.load_team", return_value=TEAM))
        router = stack.enter_context(
            patch("laya.pipeline.router.run_router", new_callable=AsyncMock, return_value=None)
        )
        rules = stack.enter_context(
            patch("laya.pipeline.rules.run_rules", new_callable=AsyncMock, return_value=(False, None))
        )
        stack.enter_context(patch("laya.api.websocket.manager.broadcast", new_callable=AsyncMock))
        stack.enter_context(
            patch("laya.pipeline.summarize.trigger_summary_status_update", new_callable=AsyncMock)
        )
        await queue.process_event(event.event_id)
    return router, rules


@pytest.mark.asyncio
async def test_process_event_self_reply_is_filtered_and_closes(db):
    await _card_for(db, _event("evt_ask", hour=9), "card_ask")
    me = _event("evt_me", hour=10, email="", handle="U_KIM", name="")
    await _store(db, me)

    router, rules = await _process(db, me)

    router.assert_not_called()
    rules.assert_not_called()
    rows = await db.execute_fetchall(
        "SELECT processing_status FROM events WHERE event_id = 'evt_me'"
    )
    assert rows[0]["processing_status"] == "filtered"
    assert (await _card(db, "card_ask"))["status"] == "done"
    assert not await db.execute_fetchall("SELECT 1 FROM action_cards WHERE event_id = 'evt_me'")


@pytest.mark.asyncio
async def test_process_event_resolved_in_source_from_other_actor(db):
    kw = dict(platform="github", subject_type="pull_request", subject_id="o/r#7")
    await _card_for(db, _event("evt_ask", hour=9, **kw), "card_ask")
    merged = _event("evt_merged", hour=10, email="", handle="someone", name="Someone",
                    metadata={"resolved_in_source": True}, raw="pr_merged", **kw)
    await _store(db, merged)

    router, _ = await _process(db, merged)

    router.assert_not_called()
    assert (await _card(db, "card_ask"))["status"] == "done"


@pytest.mark.asyncio
async def test_batch_routing_skips_conclusions(db):
    from laya.pipeline import queue

    me = _event("evt_me", hour=10, handle="U_KIM", email="", name="")
    resolved = _event("evt_res", hour=10, subject_id="C9:1.0", metadata={"resolved_in_source": True})
    a = _event("evt_a", hour=9, subject_id="C2:1.0")
    b = _event("evt_b", hour=9, subject_id="C3:1.0")
    for e in (me, resolved, a, b):
        await _store(db, e)
    with patch("laya.config.load_team", return_value=TEAM), \
         patch("laya.pipeline.ingest.load_team", return_value=TEAM), \
         patch("laya.pipeline.rules.load_rules", return_value={"rules": []}), \
         patch("laya.pipeline.router.run_batch_router", new_callable=AsyncMock, return_value={}) as batch:
        await queue._batch_route_events(["evt_me", "evt_res", "evt_a", "evt_b"])
    ids = [it["event_id"] for it in batch.await_args.args[0]]
    assert ids == ["evt_a", "evt_b"]
