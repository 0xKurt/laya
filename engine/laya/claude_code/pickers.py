# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Read-only lookups behind the settings pickers (no free-text lists in the UI).

- ``gmail_senders``: the senders seen in recent Gmail events that are not skipped
  yet, with counts, as suggestions for the "Skip mail from" chips.
- ``check_folder``: whether a typed folder exists, for the checkout folder list in
  the browser build (the desktop app uses the native folder dialog).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from laya.claude_code import store
from laya.db.sqlite import get_db
from laya.db.timeutil import db_ts


async def gmail_senders(days: int = 30, limit: int = 20) -> dict[str, Any]:
    """Distinct senders of Gmail events in the last ``days`` days, most frequent first,
    without the user's own mail and without the senders already skipped."""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    source = await store.get_source("gmail")
    config = store.merged_config("gmail", (source or {}).get("config"))
    skipped = [s.lower() for s in config.get("exclude_senders") or []]
    since = db_ts(datetime.now(timezone.utc) - timedelta(days=days))
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT LOWER(actor_email) AS email, MAX(actor_name) AS name,
                  COUNT(*) AS count, MAX(timestamp) AS last_seen
             FROM events
            WHERE source_platform = 'gmail'
              AND actor_email IS NOT NULL AND TRIM(actor_email) != ''
              AND COALESCE(actor_relationship, '') != 'self'
              AND timestamp >= ?
            GROUP BY LOWER(actor_email)
            ORDER BY count DESC, last_seen DESC""",
        (since,),
    )
    skip = set(skipped)
    senders = [
        {"email": r["email"], "name": r["name"] or "", "count": r["count"], "last_seen": r["last_seen"]}
        for r in rows
        if r["email"] not in skip and "@" in r["email"]
    ][:limit]
    return {"senders": senders, "skipped": skipped, "connected": source is not None, "days": days}


def check_folder(path: str) -> dict[str, Any]:
    """The expanded absolute form of ``path`` and whether it is an existing folder."""
    raw = (path or "").strip()
    if not raw:
        return {"path": "", "exists": False, "error": "Enter a folder path."}
    expanded = os.path.abspath(os.path.expanduser(raw))
    if not os.path.exists(expanded):
        return {"path": expanded, "exists": False, "error": "This folder does not exist."}
    if not os.path.isdir(expanded):
        return {"path": expanded, "exists": False, "error": "This is a file, not a folder."}
    return {"path": expanded.rstrip(os.sep) or os.sep, "exists": True, "error": None}
