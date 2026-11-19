# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Board API: which cards it shows and moving them between lanes."""

import pytest
from httpx import ASGITransport, AsyncClient

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
        "updated_at = datetime('now', '-10 days') WHERE card_id = 'c_old_done'"
    )
    await db.commit()

    async with await _client() as client:
        resp = await client.get("/board")

    assert resp.status_code == 200
    ids = {c["card_id"] for c in resp.json()}
    assert ids == {"c_ready", "c_agent", "c_done"}


@pytest.mark.asyncio
async def test_set_lane_round_trip(db):
    await insert_test_card(db, card_id="c1", event_id="e1", status="ready")

    async with await _client() as client:
        assert (await client.put("/cards/c1/lane", json={"lane": "todo"})).status_code == 200
        [card] = (await client.get("/board")).json()
        assert card["board_lane"] == "todo"

        await client.put("/cards/c1/lane", json={"lane": "backlog"})
        [card] = (await client.get("/board")).json()
        assert card["board_lane"] is None


@pytest.mark.asyncio
async def test_set_lane_rejects_unknown_lanes_and_cards(db):
    await insert_test_card(db, card_id="c1", event_id="e1", status="ready")

    async with await _client() as client:
        assert (await client.put("/cards/c1/lane", json={"lane": "done"})).status_code == 422
        assert (await client.put("/cards/nope/lane", json={"lane": "todo"})).status_code == 404
