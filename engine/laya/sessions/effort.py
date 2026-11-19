# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Effort estimates on cards (docs/claude-code-connector.md, "Effort and session lane").

The stager returns `effort: {you_minutes, agent_minutes, basis}`. It is kept
inside the card's `staged_output` JSON, so the card table needs no column.
The user can correct it; a corrected estimate carries `set_by = "user"`.
"""

from __future__ import annotations

import json
from typing import Any

from laya.config import load_settings
from laya.models.card import Effort

# Upper bound for one part of an estimate (a working week). A larger number is
# a model slip, not an estimate, and is clamped.
MAX_MINUTES = 40 * 60
_BASIS_MAX_CHARS = 160


def effort_estimate_enabled() -> bool:
    """Whether the stager is asked for an effort estimate.

    Settings `pipeline.effort_estimate`, on by default. Off, the effort rules
    and the schema property are left out of the stager call.
    """
    pipeline = load_settings().get("pipeline", {})
    return bool(pipeline.get("effort_estimate", True)) if isinstance(pipeline, dict) else True


def _minutes(value: Any) -> int | None:
    # bool is a subclass of int; a JSON true is not a number of minutes.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(MAX_MINUTES, int(round(value))))
    if isinstance(value, str) and value.strip().isdigit():
        return min(MAX_MINUTES, int(value.strip()))
    return None


def parse_effort(raw: Any, set_by: str = "stager") -> Effort | None:
    """Validate an effort object; None when either part is missing or not a number."""
    if not isinstance(raw, dict):
        return None
    you = _minutes(raw.get("you_minutes"))
    agent = _minutes(raw.get("agent_minutes"))
    if you is None or agent is None:
        return None
    basis = raw.get("basis")
    basis = " ".join(basis.split())[:_BASIS_MAX_CHARS] if isinstance(basis, str) else ""
    return Effort(you_minutes=you, agent_minutes=agent, basis=basis, set_by=set_by)


def format_minutes(minutes: int) -> str:
    """15 → "15m", 120 → "2h", 90 → "1h30m"."""
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest}m" if rest else f"{hours}h"


def format_effort(effort: Effort) -> str:
    """The card's effort line, e.g. "you ~30m · agent ~2h"."""
    return f"you ~{format_minutes(effort.you_minutes)} · agent ~{format_minutes(effort.agent_minutes)}"


async def staged_output_keeping_effort(db, card_id: str, staged: dict) -> str:
    """JSON for a new staged_output of `card_id` that keeps the card's effort.

    A workspace agent replaces the card's staged_output with its plan or result;
    the effort estimate lives in the same JSON and would be lost with it.
    """
    rows = await db.execute_fetchall(
        "SELECT staged_output FROM action_cards WHERE card_id = ?", (card_id,)
    )
    out = dict(staged)
    if rows and rows[0]["staged_output"]:
        try:
            old = json.loads(rows[0]["staged_output"])
        except (json.JSONDecodeError, TypeError):
            old = None
        if isinstance(old, dict) and isinstance(old.get("effort"), dict):
            out["effort"] = old["effort"]
    return json.dumps(out)
