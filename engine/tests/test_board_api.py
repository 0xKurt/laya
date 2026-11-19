# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Board API: which cards it shows, their tags, and the lane tags."""

import pytest
from httpx import ASGITransport, AsyncClient

from laya.pipeline.tags import persist_suggested_tags, select_prompt_tags
from tests.conftest import insert_test_card


async def _client():
    from laya.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_board_shows_active_and_recent_done_cards_only(db):
    await insert_test_card(db, card_id="c_ready", event_id="e1", status="ready")
    await insert_test_card(db, card_id="c_agent", event_id="e2", status="agent_running")
    await insert_test_card(db, card_id="c_done", event_id="e3", status="done")
    await insert_test_card(db, card_id="c_old_done", event_id="e4", status="done")
    await insert_test_card(db, card_id="c_archived", event_id="e5", status="archived")
    await insert_test_card(db, card_id="c_dismissed", event_id="e6", status="dismissed")
    await db.execute(
        "UPDATE action_cards SET resolved_at = datetime('now', '-10 days'), "
        "updated_at = datetime('now', '-10 days'), created_at = datetime('now', '-10 days') "
        "WHERE card_id = 'c_old_done'"
    )
    await db.commit()

    async with await _client() as client:
        resp = await client.get("/board")

    assert resp.status_code == 200
    ids = {c["card_id"] for c in resp.json()["cards"]}
    assert ids == {"c_ready", "c_agent", "c_done"}


@pytest.mark.asyncio
async def test_board_creates_lane_tags_as_system_tags_once(db):
    async with await _client() as client:
        first = (await client.get("/board")).json()["lane_tags"]
        second = (await client.get("/board")).json()["lane_tags"]

    assert first == second
    rows = await db.execute_fetchall(
        "SELECT tag_id, name, is_system FROM tags WHERE name LIKE 'lane:%' ORDER BY name"
    )
    assert [(r["name"], r["is_system"]) for r in rows] == [("lane:in-progress", 1), ("lane:todo", 1)]
    assert first == {"in_progress": rows[0]["tag_id"], "todo": rows[1]["tag_id"]}


@pytest.mark.asyncio
async def test_board_promotes_a_hand_made_lane_tag_to_system(db):
    await db.execute("INSERT INTO tags (name, is_system) VALUES ('lane:todo', 0)")
    await db.commit()

    async with await _client() as client:
        await client.get("/board")

    rows = await db.execute_fetchall("SELECT is_system FROM tags WHERE name = 'lane:todo'")
    assert rows[0]["is_system"] == 1


@pytest.mark.asyncio
async def test_lane_tag_round_trip_via_tags_api(db):
    await insert_test_card(db, card_id="c1", event_id="e1", status="ready")

    async with await _client() as client:
        lane_tags = (await client.get("/board")).json()["lane_tags"]
        resp = await client.post(
            "/tags/assign",
            json={"tag_name_or_id": lane_tags["todo"], "target_type": "card", "target_id": "c1"},
        )
        assert resp.status_code == 200
        [card] = (await client.get("/board")).json()["cards"]
        assert [t["tag_name"] for t in card["tags"]] == ["lane:todo"]
        assert card["tags"][0]["is_system"] is True

        resp = await client.request(
            "DELETE",
            "/tags/unassign",
            json={"tag_id": lane_tags["todo"], "target_type": "card", "target_id": "c1"},
        )
        assert resp.status_code == 200
        [card] = (await client.get("/board")).json()["cards"]
        assert card["tags"] == []

        # System tags cannot be deleted through the tags API.
        assert (await client.delete(f"/tags/{lane_tags['todo']}")).status_code == 403


@pytest.mark.asyncio
async def test_board_filters_by_space(db):
    await db.execute("INSERT OR IGNORE INTO spaces (space_id, name) VALUES ('work', 'Work')")
    await insert_test_card(db, card_id="c_default", event_id="e1", status="ready")
    await insert_test_card(db, card_id="c_work", event_id="e2", status="ready", space_id="work")
    await db.commit()

    async with await _client() as client:
        cards = (await client.get("/board", params={"space_id": "work"})).json()["cards"]

    assert [c["card_id"] for c in cards] == ["c_work"]


@pytest.mark.asyncio
async def test_stager_never_sees_or_assigns_lane_tags(db, sample_event):
    await insert_test_card(db, card_id="c1", event_id="e1", status="ready")
    async with await _client() as client:
        await client.get("/board")

    assert not [n for n in await select_prompt_tags(db, sample_event) if n.startswith("lane:")]
    assert await persist_suggested_tags("c1", ["lane:todo", "billing"]) == ["billing"]
