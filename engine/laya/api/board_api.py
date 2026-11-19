# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Board API — the cards of the Kanban view.

The board has no storage of its own. Its lanes come from each card's status
and two system tags (see docs/claude-code-connector.md, "Board"):

- In progress: status agent_running / awaiting_input / executing, or tag ``lane:in-progress``
- To do: tag ``lane:todo``
- Done: status done, resolved in the last DONE_VISIBLE_DAYS days
- Backlog: every other active card

The UI moves cards with the existing card routes (done, reopen, archive) and
the tags API (assign, unassign). This endpoint returns the cards with their
tags and makes sure both lane tags exist as system tags, so the UI can assign
and remove them by id.
"""

from __future__ import annotations

from fastapi import APIRouter

from laya.api.cards_common import CARD_SELECT_COLUMNS, _row_to_card
from laya.db.sqlite import get_db
from laya.models.card import TagAssignment
from laya.pipeline.tags import LANE_TAG_PREFIX, batch_load_tags

router = APIRouter()

# Done cards stay on the board this long; older ones are only in the feed.
DONE_VISIBLE_DAYS = 7
# Upper bound on the cards one board load returns.
BOARD_CARD_LIMIT = 500

LANE_TAGS = {
    "todo": f"{LANE_TAG_PREFIX}todo",
    "in_progress": f"{LANE_TAG_PREFIX}in-progress",
}
LANE_TAG_COLORS = {"todo": "#60A5FA", "in_progress": "#A78BFA"}

ACTIVE_STATUSES = (
    "pending", "ready", "requires_approval", "agent_running",
    "awaiting_input", "executing", "failed",
)


async def ensure_lane_tags() -> dict[str, int]:
    """Create the lane tags as system tags if missing; return {lane: tag_id}.

    A tag of the same name created earlier by hand is promoted to a system
    tag, so it cannot be renamed or deleted while the board relies on it.
    """
    db = await get_db()
    for lane, name in LANE_TAGS.items():
        await db.execute(
            "INSERT OR IGNORE INTO tags (name, color, is_system) VALUES (?, ?, 1)",
            (name, LANE_TAG_COLORS[lane]),
        )
        await db.execute("UPDATE tags SET is_system = 1 WHERE name = ? AND is_system = 0", (name,))
    await db.commit()
    placeholders = ",".join("?" * len(LANE_TAGS))
    rows = await db.execute_fetchall(
        f"SELECT tag_id, name FROM tags WHERE name IN ({placeholders})",
        tuple(LANE_TAGS.values()),
    )
    by_name = {r["name"]: r["tag_id"] for r in rows}
    return {lane: by_name[name] for lane, name in LANE_TAGS.items()}


@router.get("/board")
async def get_board(space_id: str | None = None) -> dict:
    """Active cards plus the cards done in the last week, each with its tags."""
    lane_tags = await ensure_lane_tags()
    db = await get_db()
    status_placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    space_filter = "AND c.space_id = ?" if space_id else ""
    rows = await db.execute_fetchall(
        f"""SELECT {CARD_SELECT_COLUMNS}
            FROM action_cards c
            LEFT JOIN events e ON c.event_id = e.event_id
            LEFT JOIN spaces s ON c.space_id = s.space_id
            WHERE (c.status IN ({status_placeholders})
                   OR (c.status = 'done'
                       AND COALESCE(c.resolved_at, c.updated_at, c.created_at)
                           >= datetime('now', ?)))
              {space_filter}
            ORDER BY c.created_at DESC
            LIMIT ?""",
        [
            *ACTIVE_STATUSES,
            f"-{DONE_VISIBLE_DAYS} days",
            *([space_id] if space_id else []),
            BOARD_CARD_LIMIT,
        ],
    )
    cards = [_row_to_card(r, slim=True) for r in rows]
    tags_map = await batch_load_tags([c.card_id for c in cards])
    for card in cards:
        card.tags = [TagAssignment(**t) for t in tags_map.get(("card", card.card_id), [])]
    return {"cards": cards, "lane_tags": lane_tags}
