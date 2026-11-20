# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""The Claude terminal as a work assistant.

The terminal runs the user's plain `claude` with Laya's MCP server added
(`claude_code/terminal.py`). Two things make that session an assistant without
a system prompt or agent of its own:

- `server_instructions()`: the MCP `instructions` Laya's server returns in its
  initialize result. Claude Code adds them to the session's context. The text
  lives in `assistant_instructions.md` next to this module and is re-read for
  every MCP session, so an edit takes effect on the next session.
- `today()`: the `today` read tool. One call returns the day's overview from
  Laya's own data: the board lanes, the cached My work data, the cards closed
  today and the open items whose last activity is old enough for a live
  re-check. It never starts a model turn or a source request: My work is read
  from its cache only.

See docs/claude-code-connector.md, "Claude terminal".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from laya.db.sqlite import get_db
from laya.db.timeutil import db_ts

log = structlog.get_logger()

INSTRUCTIONS_FILE = Path(__file__).with_name("assistant_instructions.md")

# An open item whose last activity is older than this is listed for a re-check.
STALE_HOURS = 24
# Cards per lane; Backlog shows only its top items.
LANE_LIMIT = 15
BACKLOG_LIMIT = 8
# Linear issues per state group; pull requests per group.
ISSUE_LIMIT = 15
PR_LIMIT = 15

_IN_PROGRESS_STATUSES = ("agent_running", "awaiting_input", "executing")
_ACTIVE_STATUSES = (
    "pending", "ready", "requires_approval", "agent_running",
    "awaiting_input", "executing", "failed",
)
_LANE_TODO = "lane:todo"
_LANE_IN_PROGRESS = "lane:in-progress"
_PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_LINEAR_PRIORITY = ["none", "urgent", "high", "medium", "low"]
_ISSUE_GROUPS = (("started", "in_progress"), ("unstarted", "todo"), ("backlog", "backlog"), ("triage", "triage"))


def server_instructions() -> str | None:
    """The MCP server instructions, or None when the file is missing or empty."""
    try:
        text = INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("assistant_instructions_unreadable", error=str(e))
        return None
    return text or None


# ── helpers ──────────────────────────────────────────────────────────────

def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="minutes") if dt else None


def _minutes(m: int) -> str:
    if m < 60:
        return f"{m}m"
    h, rest = divmod(m, 60)
    return f"{h}h{rest}m" if rest else f"{h}h"


def _effort(staged_output: str | None) -> dict[str, Any] | None:
    try:
        staged = json.loads(staged_output) if staged_output else None
    except (TypeError, ValueError):
        return None
    effort = staged.get("effort") if isinstance(staged, dict) else None
    if not isinstance(effort, dict):
        return None
    you, agent = effort.get("you_minutes"), effort.get("agent_minutes")
    if not isinstance(you, int) or not isinstance(agent, int):
        return None
    return {"you": _minutes(you), "agent": _minutes(agent)}


def _platform(entity_id: str | None, category: str | None) -> str:
    return entity_id.split(":", 1)[0] if entity_id else (category or "").lower()


# ── board ────────────────────────────────────────────────────────────────

async def _board_cards(db, space_id: str | None) -> list[dict[str, Any]]:
    """Active cards with lane, link, effort and the entity's last activity."""
    ph = ",".join("?" * len(_ACTIVE_STATUSES))
    space_filter = "AND c.space_id = ?" if space_id else ""
    rows = await db.execute_fetchall(
        f"""SELECT c.card_id, c.header, c.priority, c.status, c.category, c.entity_id,
                   c.source_url, c.staged_output, c.created_at, e.timestamp AS event_ts
            FROM action_cards c LEFT JOIN events e ON e.event_id = c.event_id
            WHERE c.status IN ({ph}) {space_filter}""",
        [*_ACTIVE_STATUSES, *([space_id] if space_id else [])],
    )
    if not rows:
        return []
    ids = [r["card_id"] for r in rows]
    tag_rows = await db.execute_fetchall(
        f"""SELECT ta.target_id, t.name FROM tag_assignments ta JOIN tags t ON t.tag_id = ta.tag_id
            WHERE ta.target_type = 'card' AND t.name IN (?, ?)
              AND ta.target_id IN ({",".join("?" * len(ids))})""",
        [_LANE_TODO, _LANE_IN_PROGRESS, *ids],
    )
    lane_tags: dict[str, set[str]] = {}
    for t in tag_rows:
        lane_tags.setdefault(t["target_id"], set()).add(t["name"])

    # Last activity of an entity: its newest event across all of its cards.
    entity_ids = sorted({r["entity_id"] for r in rows if r["entity_id"]})
    last_by_entity: dict[str, str] = {}
    if entity_ids:
        act_rows = await db.execute_fetchall(
            f"""SELECT c.entity_id, MAX(e.timestamp) AS last_ts
                FROM action_cards c JOIN events e ON e.event_id = c.event_id
                WHERE c.entity_id IN ({",".join("?" * len(entity_ids))})
                GROUP BY c.entity_id""",
            entity_ids,
        )
        last_by_entity = {r["entity_id"]: r["last_ts"] for r in act_rows}

    cards = []
    for r in rows:
        tags = lane_tags.get(r["card_id"], set())
        if r["status"] in _IN_PROGRESS_STATUSES or _LANE_IN_PROGRESS in tags:
            lane = "in_progress"
        elif _LANE_TODO in tags:
            lane = "todo"
        else:
            lane = "backlog"
        last = _parse_ts(last_by_entity.get(r["entity_id"]) or r["event_ts"] or r["created_at"])
        cards.append({
            "card_id": r["card_id"],
            "title": r["header"],
            "priority": r["priority"],
            "status": r["status"],
            "platform": _platform(r["entity_id"], r["category"]),
            "link": r["source_url"] or None,
            "effort": _effort(r["staged_output"]),
            "last_activity": last,
            "lane": lane,
        })
    return cards


def _sort_lane(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Priority first, then the most recent activity (the Board's order)."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        cards,
        key=lambda c: (_PRIORITY_RANK.get(c["priority"], 9), -(c["last_activity"] or epoch).timestamp()),
    )


def _card_out(card: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in card.items() if k not in ("lane",)}
    out["last_activity"] = _iso(card["last_activity"])
    return out


# ── My work (cache only) ─────────────────────────────────────────────────

async def _my_work_cache(section: str) -> dict[str, Any] | None:
    from laya.claude_code import my_work

    try:
        return await my_work._cached(section)
    except Exception as e:  # a missing table or a bad row never fails the tool
        log.warning("assistant_my_work_cache_failed", section=section, error=str(e))
        return None


def _snapshot(cache: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    fetched = _parse_ts((cache or {}).get("fetched_at"))
    age = int((now - fetched).total_seconds() // 60) if fetched else None
    return {"fetched_at": _iso(fetched), "age_minutes": age}


def _pr_group(pr: dict[str, Any]) -> str:
    """The My work view's groups (ui/src/lib/work/work.ts `prGroup`)."""
    checks = (pr.get("checks") or {}).get("state")
    changes = pr.get("review_decision") == "CHANGES_REQUESTED" or (pr.get("changes_requested") or 0) > 0
    if changes or checks == "failure" or pr.get("conflicts"):
        return "needs_action"
    if pr.get("draft"):
        return "drafts"
    approved = pr.get("review_decision") == "APPROVED" or (
        pr.get("review_decision") is None and (pr.get("approvals") or 0) > 0
    )
    if approved and checks in ("success", "none", None):
        return "ready_to_merge"
    return "waiting"


def _pr_why(pr: dict[str, Any]) -> list[str]:
    checks = pr.get("checks") or {}
    why = []
    if pr.get("review_decision") == "CHANGES_REQUESTED" or (pr.get("changes_requested") or 0) > 0:
        why.append("changes requested")
    if checks.get("state") == "failure":
        why.append(f"{checks.get('failing') or 1} checks failing")
    elif checks.get("state") == "pending":
        why.append("checks running")
    if pr.get("conflicts"):
        why.append("merge conflicts")
    if pr.get("unresolved_threads"):
        why.append(f"{pr['unresolved_threads']} unresolved threads")
    if pr.get("approvals"):
        why.append(f"{pr['approvals']} approvals")
    elif pr.get("requested_reviewers"):
        why.append("review requested")
    return why


def _github(cache: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if cache is None:
        return {"cached": False}
    groups: dict[str, list[dict[str, Any]]] = {
        "needs_action": [], "waiting": [], "ready_to_merge": [], "drafts": [],
    }
    prs = sorted(cache.get("items") or [], key=lambda p: p.get("updated_at") or "", reverse=True)
    for pr in prs:
        groups[_pr_group(pr)].append({
            "pr": f"{pr.get('repo')}#{pr.get('number')}",
            "title": pr.get("title"),
            "link": pr.get("url"),
            "state": _pr_why(pr),
            "linear_key": pr.get("linear_key"),
            "updated_at": pr.get("updated_at"),
        })
    out: dict[str, Any] = {"cached": True, **_snapshot(cache, now)}
    for name, items in groups.items():
        out[name] = items[:PR_LIMIT]
        if len(items) > PR_LIMIT:
            out[f"{name}_more"] = len(items) - PR_LIMIT
    return out


def _linear(cache: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if cache is None:
        return {"cached": False}
    issues = [i for i in (cache.get("items") or []) if i.get("assigned_to_me")]

    def rank(i: dict[str, Any]) -> tuple:
        p = i.get("priority") or 0
        return (p if 1 <= p <= 4 else 5, i.get("due_date") or "9999")

    out: dict[str, Any] = {"cached": True, **_snapshot(cache, now), "assigned": {}}
    for state_type, name in _ISSUE_GROUPS:
        # Newest update first, then (stable) priority and due date: the My work order.
        group = sorted((i for i in issues if i.get("state_type") == state_type),
                       key=lambda i: i.get("updated_at") or "", reverse=True)
        group.sort(key=rank)
        if not group:
            continue
        out["assigned"][name] = [
            {
                "id": i.get("identifier"),
                "title": i.get("title"),
                "link": i.get("url"),
                "state": i.get("state"),
                "priority": _LINEAR_PRIORITY[i["priority"]] if 0 <= (i.get("priority") or 0) <= 4 else "none",
                "due": i.get("due_date"),
                "project": i.get("project"),
                "updated_at": i.get("updated_at"),
            }
            for i in group[:ISSUE_LIMIT]
        ]
        if len(group) > ISSUE_LIMIT:
            out["assigned"][f"{name}_more"] = len(group) - ISSUE_LIMIT
    out["created_by_me_open"] = sum(
        1 for i in (cache.get("items") or []) if i.get("created_by_me") and not i.get("assigned_to_me")
    )
    return out


# ── closed today ─────────────────────────────────────────────────────────

async def _closed_today(db, space_id: str | None, now: datetime) -> list[dict[str, Any]]:
    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    space_filter = "AND space_id = ?" if space_id else ""
    rows = await db.execute_fetchall(
        f"""SELECT card_id, header, source_url, status_reason, status_set_by,
                   COALESCE(resolved_at, updated_at) AS closed_at
            FROM action_cards
            WHERE status = 'done' AND COALESCE(resolved_at, updated_at) >= ? {space_filter}
            ORDER BY closed_at DESC LIMIT 30""",
        [db_ts(local_midnight), *([space_id] if space_id else [])],
    )
    return [
        {
            "card_id": r["card_id"],
            "title": r["header"],
            "link": r["source_url"] or None,
            "closed_at": _iso(_parse_ts(r["closed_at"])),
            "closed_by": "laya" if r["status_set_by"] == "laya" else "user",
            "reason": r["status_reason"] or None,
        }
        for r in rows
    ]


# ── the tool ─────────────────────────────────────────────────────────────

async def today(space_id: str | None = None) -> dict[str, Any]:
    """The day's overview for the `today` read tool. Pure data, read from Laya only."""
    db = await get_db()
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(hours=STALE_HOURS)

    cards = await _board_cards(db, space_id)
    lanes: dict[str, list[dict[str, Any]]] = {"in_progress": [], "todo": [], "backlog": []}
    for card in cards:
        lanes[card["lane"]].append(card)
    in_progress = _sort_lane(lanes["in_progress"])
    todo = _sort_lane(lanes["todo"])
    backlog = _sort_lane(lanes["backlog"])

    recheck = [
        {
            "card_id": c["card_id"],
            "title": c["title"],
            "link": c["link"],
            "lane": c["lane"],
            "last_activity": _iso(c["last_activity"]),
            "idle_hours": int((now - c["last_activity"]).total_seconds() // 3600),
        }
        for c in _sort_lane([*in_progress, *todo, *backlog[:BACKLOG_LIMIT]])
        if c["last_activity"] and c["last_activity"] < stale_before
    ]

    github, linear = await _my_work_cache("github"), await _my_work_cache("linear")
    return {
        "now": _iso(now),
        "stale_after_hours": STALE_HOURS,
        "board": {
            "in_progress": [_card_out(c) for c in in_progress[:LANE_LIMIT]],
            "todo": [_card_out(c) for c in todo[:LANE_LIMIT]],
            "backlog_top": [_card_out(c) for c in backlog[:BACKLOG_LIMIT]],
            "counts": {k: len(v) for k, v in lanes.items()},
        },
        "my_work": {"linear": _linear(linear, now), "github": _github(github, now)},
        "closed_today": await _closed_today(db, space_id, now),
        "recheck": recheck,
    }
