# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Board API — the cards of the Kanban view and their lanes."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from laya.api.cards_common import CARD_SELECT_COLUMNS, _row_to_card
from laya.api.websocket import manager
from laya.db.sqlite import get_db
from laya.models.card import CardResponse

router = APIRouter()

# Done cards stay on the board this long; older ones are only in the feed.
DONE_VISIBLE_DAYS = 7


class LaneRequest(BaseModel):
    lane: Literal["backlog", "todo", "in_progress"]


@router.get("/board", response_model=list[CardResponse])
async def get_board(space_id: str | None = None) -> list[CardResponse]:
    """Every active card plus the cards done in the last week, newest activity first."""
    db = await get_db()
    space_filter = "AND c.space_id = ?" if space_id else ""
    rows = await db.execute_fetchall(
        f"""SELECT {CARD_SELECT_COLUMNS}
            FROM action_cards c
            LEFT JOIN events e ON c.event_id = e.event_id
            LEFT JOIN spaces s ON c.space_id = s.space_id
            WHERE (c.status IN ('pending', 'ready', 'requires_approval', 'agent_running',
                                'awaiting_input', 'executing', 'failed')
                   OR (c.status = 'done'
                       AND COALESCE(c.resolved_at, c.updated_at, c.created_at)
                           >= datetime('now', ?)))
              {space_filter}
            ORDER BY COALESCE(e.timestamp, c.created_at) DESC
            LIMIT 500""",
        [f"-{DONE_VISIBLE_DAYS} days", *([space_id] if space_id else [])],
    )
    return [_row_to_card(r, slim=True) for r in rows]


@router.put("/cards/{card_id}/lane")
async def set_card_lane(card_id: str, body: LaneRequest) -> dict:
    """Move a card to a board lane. Status changes (done, archive) use their own routes."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE action_cards SET board_lane = ? WHERE card_id = ?",
        (None if body.lane == "backlog" else body.lane, card_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Card not found")
    await manager.broadcast(
        {"type": "card_updated", "card_id": card_id, "payload": {"board_lane": body.lane}}
    )
    return {"card_id": card_id, "lane": body.lane}
