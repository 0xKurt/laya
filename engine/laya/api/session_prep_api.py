# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Effort and session lane endpoints (docs/claude-code-connector.md, "Effort and session lane").

- ``PUT  /cards/{card_id}/effort``: the user's correction of a card's effort
  estimate, stored in the card's staged_output JSON.
- ``POST /cards/{card_id}/session-prep``: a session prompt and worktree
  commands for a coding session the user starts himself. Nothing is started.
- ``GET  /cards/{card_id}/session-claims``: "merged" / "green" claims in the
  card's text, each checked with ``gh``.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laya.api.cards_lifecycle import get_card
from laya.api.websocket import manager
from laya.db.sqlite import get_db
from laya.sessions.effort import MAX_MINUTES, parse_effort
from laya.sessions.prep import card_text, github_repo_of, prepare_session
from laya.sessions.verify import has_claim, verify_claims

router = APIRouter()


class EffortUpdate(BaseModel):
    you_minutes: int = Field(ge=0, le=MAX_MINUTES)
    agent_minutes: int = Field(ge=0, le=MAX_MINUTES)
    basis: str = ""


async def _event_metadata(card_id: str) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT e.content_metadata FROM action_cards c
           JOIN events e ON e.event_id = c.event_id WHERE c.card_id = ?""",
        (card_id,),
    )
    if not rows or not rows[0]["content_metadata"]:
        return {}
    try:
        meta = json.loads(rows[0]["content_metadata"])
    except (json.JSONDecodeError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


@router.put("/cards/{card_id}/effort")
async def update_effort(card_id: str, body: EffortUpdate) -> dict:
    """Store the user's effort estimate for a card (``set_by = "user"``)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT staged_output FROM action_cards WHERE card_id = ?", (card_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Card not found")
    try:
        staged = json.loads(rows[0]["staged_output"] or "{}")
    except (json.JSONDecodeError, TypeError):
        staged = {}
    if not isinstance(staged, dict):
        staged = {}
    # A card without a stager output still needs the fields StagedOutput requires.
    staged.setdefault("type", "summary")
    staged.setdefault("content", "")
    effort = parse_effort(body.model_dump(), set_by="user")
    staged["effort"] = effort.model_dump()
    await db.execute(
        "UPDATE action_cards SET staged_output = ?, updated_at = CURRENT_TIMESTAMP WHERE card_id = ?",
        (json.dumps(staged), card_id),
    )
    await db.commit()
    await manager.broadcast(
        {"type": "card_updated", "card_id": card_id, "payload": {"effort": staged["effort"]}}
    )
    return {"card_id": card_id, "effort": staged["effort"]}


@router.post("/cards/{card_id}/session-prep")
async def session_prep(card_id: str) -> dict:
    """Session prompt + worktree commands for the card. Starts nothing."""
    card = await get_card(card_id)
    prep = await prepare_session(card, await _event_metadata(card.card_id))
    return prep.to_dict()


@router.get("/cards/{card_id}/session-claims")
async def session_claims(card_id: str) -> dict:
    """The card's merged/green claims, each checked against gh."""
    card = await get_card(card_id)
    text = card_text(card)
    if not has_claim(text):
        return {"card_id": card.card_id, "claims": []}
    default_repo = github_repo_of(card, await _event_metadata(card.card_id))
    return {"card_id": card.card_id, "claims": await verify_claims(text, default_repo)}
