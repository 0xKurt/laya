# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Module-owned tables of the Claude Code connector and their helpers.

The connector keeps its state in two tables it creates itself with
`CREATE TABLE IF NOT EXISTS` (never through a numbered migration, so the
upstream migration sequence stays untouched):

- `claude_code_sources`: one row per connected platform (config, discovered
  tools, the user's identity, the sweep cursor and the last run's outcome);
- `claude_code_state`: free key/value memory per platform (e.g. the Slack
  threads the user wrote in).

Also holds the per-platform config defaults (docs/claude-code-connector.md,
"Config").
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from laya.db.sqlite import get_db

PLATFORMS: tuple[str, ...] = ("slack", "gmail", "linear", "github", "notion")

LABELS: dict[str, str] = {
    "slack": "Slack",
    "gmail": "Gmail",
    "linear": "Linear",
    "github": "GitHub",
    "notion": "Notion",
}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS claude_code_sources (
        platform     TEXT PRIMARY KEY,
        enabled      INTEGER NOT NULL DEFAULT 1,
        config       TEXT NOT NULL DEFAULT '{}',
        tools        TEXT NOT NULL DEFAULT '{}',
        identity     TEXT NOT NULL DEFAULT '{}',
        cursor       TEXT,
        last_run_at  TEXT, last_ok_at TEXT, last_error TEXT, last_count INTEGER,
        connected_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS claude_code_state (
        platform TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY (platform, key)
    )""",
)

_JSON_COLUMNS = ("config", "tools", "identity")

COMMON_DEFAULTS: dict[str, Any] = {
    "interval_minutes": 60,
    "active_hours": {"start": "08:00", "end": "19:00", "days": [0, 1, 2, 3, 4]},
    # Empty means the system's local time zone.
    "timezone": "",
    "model": "haiku",
    "initial_lookback_hours": 12,
    "recheck_after_hours": 6,
}

PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    "slack": {"context_messages": 8, "thread_memory_days": 14, "only_relevant": True},
    "gmail": {
        "exclude_senders": [
            "notifications@linear.app",
            "notifications@github.com",
            "noreply@github.com",
            "notify@mail.notion.com",
        ],
        "meeting_notes": True,
    },
    "github": {"orgs": [], "others_prs_only_on_mention": True},
    "linear": {},
    "notion": {},
}


def default_config(platform: str) -> dict[str, Any]:
    return copy.deepcopy({**COMMON_DEFAULTS, **PLATFORM_DEFAULTS.get(platform, {})})


def merged_config(platform: str, stored: dict[str, Any] | None) -> dict[str, Any]:
    """Stored config over the defaults; unknown keys are dropped."""
    config = default_config(platform)
    for key, value in (stored or {}).items():
        if key not in config:
            continue
        if key == "active_hours" and isinstance(value, dict):
            config[key] = {**config[key], **value}
        else:
            config[key] = value
    return config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The connection whose tables were already created. Tests open a fresh
# in-memory connection per test, so the check is per connection object.
_ensured_for: int | None = None


async def ensure_tables(db=None) -> None:
    global _ensured_for
    db = db or await get_db()
    if _ensured_for == id(db):
        return
    for sql in _SCHEMA:
        await db.execute(sql)
    await db.commit()
    _ensured_for = id(db)


async def _db():
    db = await get_db()
    await ensure_tables(db)
    return db


def _row_to_source(row) -> dict[str, Any]:
    source = dict(row)
    for col in _JSON_COLUMNS:
        try:
            source[col] = json.loads(source.get(col) or "{}")
        except (TypeError, ValueError):
            source[col] = {}
    source["enabled"] = bool(source.get("enabled"))
    source["config"] = merged_config(source["platform"], source["config"])
    return source


async def get_source(platform: str) -> dict[str, Any] | None:
    db = await _db()
    rows = await db.execute_fetchall(
        "SELECT * FROM claude_code_sources WHERE platform = ?", (platform,)
    )
    return _row_to_source(rows[0]) if rows else None


async def list_sources() -> list[dict[str, Any]]:
    db = await _db()
    rows = await db.execute_fetchall("SELECT * FROM claude_code_sources ORDER BY platform")
    return [_row_to_source(r) for r in rows]


async def upsert_source(platform: str, **fields: Any) -> None:
    """Insert the platform's row or update the given columns of it."""
    db = await _db()
    values = {
        k: (json.dumps(v) if k in _JSON_COLUMNS else (int(v) if k == "enabled" else v))
        for k, v in fields.items()
    }
    await db.execute(
        "INSERT OR IGNORE INTO claude_code_sources (platform) VALUES (?)", (platform,)
    )
    if values:
        assignments = ", ".join(f"{k} = ?" for k in values)
        await db.execute(
            f"UPDATE claude_code_sources SET {assignments} WHERE platform = ?",
            (*values.values(), platform),
        )
    await db.commit()


async def delete_source(platform: str) -> None:
    db = await _db()
    await db.execute("DELETE FROM claude_code_sources WHERE platform = ?", (platform,))
    await db.execute("DELETE FROM claude_code_state WHERE platform = ?", (platform,))
    await db.commit()


async def record_run(
    platform: str,
    *,
    started_at: str,
    ok: bool,
    count: int = 0,
    error: str | None = None,
) -> None:
    """Store a sweep's outcome. A successful sweep moves the cursor to its start."""
    fields: dict[str, Any] = {"last_run_at": started_at, "last_error": error, "last_count": count}
    if ok:
        fields.update(cursor=started_at, last_ok_at=now_iso())
    await upsert_source(platform, **fields)


# ── key/value state ──────────────────────────────────────────────────────


async def get_state(platform: str, prefix: str = "") -> dict[str, str]:
    db = await _db()
    rows = await db.execute_fetchall(
        "SELECT key, value FROM claude_code_state WHERE platform = ? AND key LIKE ?",
        (platform, f"{prefix}%"),
    )
    return {r["key"]: r["value"] for r in rows}


async def set_state(platform: str, key: str, value: str) -> None:
    db = await _db()
    await db.execute(
        """INSERT INTO claude_code_state (platform, key, value) VALUES (?, ?, ?)
           ON CONFLICT(platform, key) DO UPDATE SET value = excluded.value""",
        (platform, key, value),
    )
    await db.commit()


async def delete_state(platform: str, keys: list[str]) -> None:
    if not keys:
        return
    db = await _db()
    await db.executemany(
        "DELETE FROM claude_code_state WHERE platform = ? AND key = ?",
        [(platform, k) for k in keys],
    )
    await db.commit()
