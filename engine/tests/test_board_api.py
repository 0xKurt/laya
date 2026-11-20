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


async def _lane_ids(client, lane):
    cards = (await client.get("/board")).json()["cards"]
    return [c["card_id"] for c in cards if c["lane"] == lane]


@pytest.mark.asyncio
async def test_new_cards_sort_by_priority_then_activity_and_have_no_position(db):
    await insert_test_card(db, card_id="low", event_id="e1", status="ready", priority="LOW")
    await insert_test_card(db, card_id="crit", event_id="e2", status="ready", priority="CRITICAL")
    await insert_test_card(db, card_id="med_old", event_id="e3", status="ready", priority="MEDIUM")
    await insert_test_card(db, card_id="med_new", event_id="e4", status="ready", priority="MEDIUM")
    await db.execute("UPDATE action_cards SET created_at = datetime('now', '-2 days') WHERE card_id = 'med_old'")
    await db.commit()

    async with await _client() as client:
        cards = (await client.get("/board")).json()["cards"]

    assert [c["card_id"] for c in cards] == ["crit", "med_new", "med_old", "low"]
    assert {c["lane"] for c in cards} == {"backlog"}
    assert all(c["position"] is None for c in cards)


@pytest.mark.asyncio
async def test_put_order_persists_and_new_cards_go_on_top(db):
    for i, cid in enumerate(["a", "b", "c"]):
        await insert_test_card(db, card_id=cid, event_id=f"e{i}", status="ready")

    async with await _client() as client:
        resp = await client.put("/board/order", json={"lane": "backlog", "card_ids": ["c", "a", "b", "ghost"]})
        assert resp.status_code == 200
        assert resp.json()["card_ids"] == ["c", "a", "b"]  # unknown ids skipped
        cards = (await client.get("/board")).json()["cards"]
        assert [(c["card_id"], c["position"]) for c in cards] == [("c", 0.0), ("a", 1.0), ("b", 2.0)]

        await insert_test_card(db, card_id="new", event_id="e9", status="ready", priority="LOW")
        assert await _lane_ids(client, "backlog") == ["new", "c", "a", "b"]

        # A second order for the lane replaces the first; cards left out lose their place.
        await client.put("/board/order", json={"lane": "backlog", "card_ids": ["b", "new"]})
        rows = await db.execute_fetchall("SELECT card_id FROM board_positions ORDER BY position")
        assert [r["card_id"] for r in rows] == ["b", "new"]


@pytest.mark.asyncio
async def test_position_only_counts_in_the_lane_it_was_set_in(db):
    await insert_test_card(db, card_id="a", event_id="e1", status="ready")
    await insert_test_card(db, card_id="b", event_id="e2", status="ready")

    async with await _client() as client:
        await client.put("/board/order", json={"lane": "backlog", "card_ids": ["a", "b"]})
        # An agent starts on "a": it is In progress now, its Backlog place is stale.
        await db.execute("UPDATE action_cards SET status = 'agent_running' WHERE card_id = 'a'")
        await db.commit()
        cards = {c["card_id"]: c for c in (await client.get("/board")).json()["cards"]}

    assert cards["a"]["lane"] == "in_progress" and cards["a"]["position"] is None
    assert cards["b"]["lane"] == "backlog" and cards["b"]["position"] == 1.0


@pytest.mark.asyncio
async def test_order_after_a_lane_change_via_tags(db):
    await insert_test_card(db, card_id="a", event_id="e1", status="ready")
    await insert_test_card(db, card_id="b", event_id="e2", status="ready")

    async with await _client() as client:
        lane_tags = (await client.get("/board")).json()["lane_tags"]
        for cid in ("a", "b"):
            await client.post(
                "/tags/assign",
                json={"tag_name_or_id": lane_tags["todo"], "target_type": "card", "target_id": cid},
            )
        await client.put("/board/order", json={"lane": "todo", "card_ids": ["b", "a"]})
        assert await _lane_ids(client, "todo") == ["b", "a"]


@pytest.mark.asyncio
async def test_done_is_newest_first_until_ordered_by_hand(db):
    await insert_test_card(db, card_id="d_old", event_id="e1", status="done")
    await insert_test_card(db, card_id="d_new", event_id="e2", status="done")
    await db.execute("UPDATE action_cards SET resolved_at = datetime('now', '-2 days') WHERE card_id = 'd_old'")
    await db.execute("UPDATE action_cards SET resolved_at = datetime('now', '-1 hours') WHERE card_id = 'd_new'")
    await db.commit()

    async with await _client() as client:
        assert await _lane_ids(client, "done") == ["d_new", "d_old"]
        resp = await client.put("/board/order", json={"lane": "done", "card_ids": ["d_old", "d_new"]})
        assert resp.status_code == 200
        assert await _lane_ids(client, "done") == ["d_old", "d_new"]

        # A card marked done outside the board goes on top once ...
        await insert_test_card(db, card_id="d_late", event_id="e3", status="ready")
        await client.post("/cards/d_late/done")
        assert await _lane_ids(client, "done") == ["d_late", "d_old", "d_new"]
        # ... and the user can place it anywhere afterwards.
        await client.put("/board/order", json={"lane": "done", "card_ids": ["d_old", "d_late", "d_new"]})
        assert await _lane_ids(client, "done") == ["d_old", "d_late", "d_new"]

        resp = await client.put("/board/order", json={"lane": "archive", "card_ids": ["d_old"]})
        assert resp.status_code == 400
        resp = await client.put("/board/order", json={"lane": "backlog", "card_ids": ["x", "x"]})
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_newcomers_go_on_top_once_and_never_above_later_arrivals(db):
    for i, cid in enumerate(["a", "b", "c"]):
        await insert_test_card(db, card_id=cid, event_id=f"e{i}", status="ready")

    async with await _client() as client:
        await client.put("/board/order", json={"lane": "backlog", "card_ids": ["b", "c", "a"]})
        await insert_test_card(db, card_id="n1", event_id="e5", status="ready", priority="LOW")
        assert await _lane_ids(client, "backlog") == ["n1", "b", "c", "a"]
        # The newcomer's place is stored, so it does not float above the next one.
        rows = await db.execute_fetchall("SELECT lane FROM board_positions WHERE card_id = 'n1'")
        assert [r["lane"] for r in rows] == ["backlog"]
        await insert_test_card(db, card_id="n2", event_id="e6", status="ready", priority="LOW")
        assert await _lane_ids(client, "backlog") == ["n2", "n1", "b", "c", "a"]
        # The user drops n2 below a: it stays there across loads.
        await client.put("/board/order", json={"lane": "backlog", "card_ids": ["n1", "b", "c", "a", "n2"]})
        assert await _lane_ids(client, "backlog") == ["n1", "b", "c", "a", "n2"]
        assert await _lane_ids(client, "backlog") == ["n1", "b", "c", "a", "n2"]


@pytest.mark.asyncio
async def test_lane_without_manual_order_gets_no_positions(db):
    await insert_test_card(db, card_id="a", event_id="e1", status="ready")
    await insert_test_card(db, card_id="d", event_id="e2", status="done")

    async with await _client() as client:
        await client.get("/board")

    rows = await db.execute_fetchall("SELECT card_id FROM board_positions")
    assert rows == []


@pytest.mark.asyncio
async def test_a_card_moved_by_status_goes_on_top_of_its_new_lane(db):
    for i, cid in enumerate(["a", "b", "x"]):
        await insert_test_card(db, card_id=cid, event_id=f"e{i}", status="ready")

    async with await _client() as client:
        await client.put("/board/order", json={"lane": "backlog", "card_ids": ["a", "b", "x"]})
        await client.put("/board/order", json={"lane": "done", "card_ids": []})
        await client.post("/cards/x/done")
        await insert_test_card(db, card_id="d1", event_id="e9", status="done")
        await client.put("/board/order", json={"lane": "done", "card_ids": ["d1"]})
        assert await _lane_ids(client, "done") == ["x", "d1"]
        rows = await db.execute_fetchall("SELECT lane FROM board_positions WHERE card_id = 'x'")
        assert [r["lane"] for r in rows] == ["done"]


@pytest.mark.asyncio
async def test_positions_of_archived_old_done_and_deleted_cards_are_pruned(db):
    for i, cid in enumerate(["keep", "arch", "done", "old_done", "gone"]):
        await insert_test_card(db, card_id=cid, event_id=f"e{i}", status="ready")

    async with await _client() as client:
        await client.put("/board/order", json={"lane": "backlog", "card_ids": ["keep", "arch", "done", "old_done", "gone"]})
        await db.execute("UPDATE action_cards SET status = 'archived' WHERE card_id = 'arch'")
        await db.execute("UPDATE action_cards SET status = 'done', resolved_at = datetime('now') WHERE card_id = 'done'")
        await db.execute(
            "UPDATE action_cards SET status = 'done', resolved_at = datetime('now', '-10 days') WHERE card_id = 'old_done'"
        )
        await db.execute("DELETE FROM action_cards WHERE card_id = 'gone'")
        await db.commit()
        await client.get("/board")

    rows = await db.execute_fetchall("SELECT card_id FROM board_positions ORDER BY card_id")
    assert [r["card_id"] for r in rows] == ["done", "keep"]
