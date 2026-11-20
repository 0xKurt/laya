# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Board API — the cards of the Kanban view.

A card's lane comes from its status and two system tags (see
docs/claude-code-connector.md, "Board"):

- In progress: status agent_running / awaiting_input / executing, or tag ``lane:in-progress``
- To do: tag ``lane:todo``
- Done: status done, resolved in the last DONE_VISIBLE_DAYS days
- Backlog: every other active card

The UI moves cards with the existing card routes (done, reopen, archive) and
the tags API (assign, unassign). This endpoint returns the cards with their
tags and makes sure both lane tags exist as system tags, so the UI can assign
and remove them by id.

The only state the board keeps itself is the manual order of the cards in
every lane: table ``board_positions`` (created by this module on first use,
one row per placed card with the lane it was placed in). A position counts
only while the card is still in that lane.

A lane without any position keeps its default order: Backlog, To do and In
progress by priority, then latest activity; Done newest first. Once a lane has
a manual order, a card that arrives without a position (a new card, a card
moved by a status change or marked done outside the board) goes on top of it
once: GET /board gives it a position above the lane's first card and stores
it, so the next newcomer lands above it instead of it floating above the
cards the user placed.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laya.api.cards_common import CARD_SELECT_COLUMNS, _row_to_card
from laya.db.sqlite import get_db, transaction
from laya.models.card import CardResponse, TagAssignment
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
# Statuses that put a card in In progress whatever its lane tags say.
IN_PROGRESS_STATUSES = {"agent_running", "awaiting_input", "executing"}
# Lanes the user orders by hand.
ORDERED_LANES = ("backlog", "todo", "in_progress", "done")
PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

POSITIONS_DDL = """CREATE TABLE IF NOT EXISTS board_positions (
    card_id    TEXT PRIMARY KEY,
    lane       TEXT NOT NULL,
    position   REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""


async def ensure_positions_table() -> None:
    """Create the board's own order table if missing (not part of the migrations)."""
    db = await get_db()
    await db.execute(POSITIONS_DDL)


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


def lane_of(card: CardResponse) -> str:
    """The lane a card shows in; the same rules as ``laneOf`` in ui/src/lib/board/board.ts."""
    names = {t.tag_name for t in card.tags or []}
    if card.status == "done":
        return "done"
    if card.status in IN_PROGRESS_STATUSES or LANE_TAGS["in_progress"] in names:
        return "in_progress"
    if LANE_TAGS["todo"] in names:
        return "todo"
    return "backlog"


def last_activity(card: dict) -> str:
    """The card's latest activity time as a sortable string."""
    return max(
        card.get("group_active_at") or "",
        card.get("event_timestamp") or "",
        card.get("created_at") or "",
    )


def default_sort_key(card: dict) -> tuple:
    """Order of the cards without a position: Done newest first; the other
    lanes by priority, then latest activity."""
    if card["lane"] == "done":
        done_at = card.get("resolved_at") or card.get("updated_at") or ""
        return (_desc(done_at), _desc(card.get("created_at") or ""))
    return (PRIORITY_RANK.get(card.get("priority"), 9), _desc(last_activity(card)))


def board_sort_key(card: dict) -> tuple:
    """Sort key within a lane: cards without a position first (default
    order), then the placed cards by position."""
    if card["position"] is None:
        return (0, default_sort_key(card))
    return (1, card["position"])


def place_newcomers(cards: list[dict]) -> list[tuple[str, str, float]]:
    """Give the unplaced cards of each lane that has a manual order a position
    above its first placed card (in default order); returns the new rows
    (card_id, lane, position) and sets them on the cards."""
    rows: list[tuple[str, str, float]] = []
    for lane in ORDERED_LANES:
        in_lane = [c for c in cards if c["lane"] == lane]
        placed = [c["position"] for c in in_lane if c["position"] is not None]
        if not placed:
            continue
        newcomers = sorted((c for c in in_lane if c["position"] is None), key=default_sort_key)
        top = min(placed)
        for i, card in enumerate(newcomers):
            card["position"] = top - len(newcomers) + i
            rows.append((card["card_id"], lane, float(card["position"])))
    return rows


def _desc(text: str) -> tuple:
    """Key that sorts strings in descending order inside an ascending sort."""
    return tuple(-ord(ch) for ch in text) + (1,)


async def _prune_positions() -> None:
    """Drop positions of cards that left the board (archived, dismissed,
    deleted, or done longer ago than Done shows)."""
    db = await get_db()
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    await db.execute(
        f"""DELETE FROM board_positions
            WHERE card_id NOT IN (
                SELECT card_id FROM action_cards
                WHERE status IN ({placeholders})
                   OR (status = 'done'
                       AND COALESCE(resolved_at, updated_at, created_at) >= datetime('now', ?)))""",
        [*ACTIVE_STATUSES, f"-{DONE_VISIBLE_DAYS} days"],
    )
    await db.commit()


@router.get("/board")
async def get_board(space_id: str | None = None) -> dict:
    """Active cards plus the cards done in the last week, each with its tags,
    its lane and its manual position, sorted per lane."""
    lane_tags = await ensure_lane_tags()
    await ensure_positions_table()
    await _prune_positions()
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
    placed = {
        r["card_id"]: (r["lane"], r["position"])
        for r in await db.execute_fetchall("SELECT card_id, lane, position FROM board_positions")
    }
    out = []
    for card in cards:
        lane = lane_of(card)
        stored = placed.get(card.card_id)
        item = card.model_dump(mode="json")
        item["lane"] = lane
        # A position from another lane is stale: the card moved by a status change.
        item["position"] = stored[1] if stored and stored[0] == lane else None
        out.append(item)
    newcomers = place_newcomers(out)
    if newcomers:
        # A position set meanwhile in the card's current lane (a drop that
        # raced this load) wins over the newcomer's place.
        await db.executemany(
            """INSERT INTO board_positions (card_id, lane, position, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(card_id) DO UPDATE SET
                   lane = excluded.lane, position = excluded.position,
                   updated_at = excluded.updated_at
               WHERE board_positions.lane != excluded.lane""",
            newcomers,
        )
        await db.commit()
    out.sort(key=lambda c: (LANE_ORDER[c["lane"]], board_sort_key(c)))
    return {"cards": out, "lane_tags": lane_tags}


LANE_ORDER = {"backlog": 0, "todo": 1, "in_progress": 2, "done": 3}
MAX_ORDER_IDS = 2000


class LaneOrder(BaseModel):
    lane: str
    card_ids: list[str] = Field(max_length=MAX_ORDER_IDS)


@router.put("/board/order")
async def put_board_order(body: LaneOrder) -> dict:
    """Store the full order of one lane, top first, as shown after a drop.

    Cards listed get positions 0..n-1 in that lane; other cards placed in that
    lane lose their position (they have left it). Unknown card ids are skipped.
    """
    if body.lane not in ORDERED_LANES:
        raise HTTPException(400, f"lane must be one of {', '.join(ORDERED_LANES)}")
    if len(set(body.card_ids)) != len(body.card_ids):
        raise HTTPException(400, "card_ids contains duplicates")
    await ensure_positions_table()
    db = await get_db()
    known: set[str] = set()
    for i in range(0, len(body.card_ids), 500):
        chunk = body.card_ids[i:i + 500]
        rows = await db.execute_fetchall(
            f"SELECT card_id FROM action_cards WHERE card_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        known.update(r["card_id"] for r in rows)
    ids = [cid for cid in body.card_ids if cid in known]
    async with transaction() as tx:
        await tx.execute("DELETE FROM board_positions WHERE lane = ?", (body.lane,))
        await tx.executemany(
            """INSERT INTO board_positions (card_id, lane, position, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(card_id) DO UPDATE SET
                   lane = excluded.lane, position = excluded.position,
                   updated_at = excluded.updated_at""",
            [(cid, body.lane, float(pos)) for pos, cid in enumerate(ids)],
        )
    return {"lane": body.lane, "card_ids": ids}
