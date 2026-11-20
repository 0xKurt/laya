# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Codex logins: usage limits of the Codex CLI (ChatGPT subscription).

Codex reports a reached usage limit only as an error of the turn
(``{"type":"error"}`` / ``{"type":"turn.failed"}`` in ``codex exec --json``),
with the reset time in words ("… or try again at 10:14 AM."). This module
turns that message into a ``rate_limit_info`` shaped like Claude Code's
(``{"status": "rejected", "resetsAt": <epoch>}``) and stores it in
``agent_rate_limit_state`` under ``codex_cli@<login>``, so the agent budget
and the routing read Codex logins exactly like Claude accounts.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger()

AGENT_ID = "codex_cli"
# The Codex CLI's own login (no CODEX_HOME override).
DEFAULT_ID = "codex-default"

# A limit message without a reset time blocks the login this long.
REJECTION_HOLD_SECONDS = 1800

_LIMIT_RE = re.compile(
    r"usage limit|hit your (?:usage )?limit|rate[ _-]?limit(?:ed)? (?:reached|exceeded)|"
    r"out of codex messages|quota exceeded",
    re.IGNORECASE,
)
# "try again at 10:14 AM" / "try again at 3:04 PM" / "try again at 15:04"
_AT_TIME_RE = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?", re.IGNORECASE)
# "try again at Nov 22nd, 2026 3:04 PM"
_AT_DATE_RE = re.compile(
    r"try again at\s+([A-Z][a-z]{2,8})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?",
)
# "try again in 2 days 3 hours 5 minutes"
_IN_RE = re.compile(r"try again in\s+((?:\d+\s*(?:days?|hours?|minutes?|mins?|seconds?|secs?)[\s,and]*)+)", re.IGNORECASE)
_PART_RE = re.compile(r"(\d+)\s*(day|hour|min|sec)", re.IGNORECASE)


def is_limit_message(text: str | None) -> bool:
    return bool(text and _LIMIT_RE.search(text))


def _hour24(hour: int, ampm: str | None) -> int:
    if not ampm:
        return hour
    pm = ampm.lower().startswith("p")
    hour = hour % 12
    return hour + 12 if pm else hour


def parse_reset(text: str, now: float | None = None) -> int | None:
    """The reset time (unix seconds) a Codex limit message names, or None.

    A clock time without a date is the next such time in local time (today,
    or tomorrow when it has passed).
    """
    now = time.time() if now is None else now
    base = datetime.fromtimestamp(now)
    m = _AT_DATE_RE.search(text)
    if m:
        month, day, year, hh, mm, ampm = m.groups()
        try:
            month_no = datetime.strptime(month[:3], "%b").month
            dt = datetime(int(year), month_no, int(day), _hour24(int(hh), ampm), int(mm))
        except ValueError:
            return None
        return int(dt.timestamp())
    m = _AT_TIME_RE.search(text)
    if m:
        hh, mm, ampm = m.groups()
        try:
            dt = base.replace(hour=_hour24(int(hh), ampm), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            return None
        if dt.timestamp() <= now:
            dt += timedelta(days=1)
        return int(dt.timestamp())
    m = _IN_RE.search(text)
    if m:
        seconds = 0
        for n, unit in _PART_RE.findall(m.group(1)):
            seconds += int(n) * {"day": 86400, "hour": 3600, "min": 60, "sec": 1}[unit.lower()]
        return int(now) + seconds if seconds else None
    return None


def limit_info(error: str | None, now: float | None = None) -> dict[str, Any] | None:
    """A Claude-shaped ``rate_limit_info`` for a Codex limit error, else None."""
    if not is_limit_message(error):
        return None
    now = time.time() if now is None else now
    reset = parse_reset(error or "", now)
    return {
        "status": "rejected",
        "resetsAt": reset or int(now) + REJECTION_HOLD_SECONDS,
        "source": "codex_error",
        "message": (error or "")[:300],
    }


def agent_row(login_id: str) -> str:
    """The ``agent_rate_limit_state`` row of a Codex login."""
    return f"{AGENT_ID}@{login_id}"


async def rate_row(login_id: str) -> str:
    return agent_row(login_id or DEFAULT_ID)


async def record(login_id: str | None, info: dict | None) -> None:
    """Store a Codex turn's limit signal under the login's row. Never raises."""
    if not info:
        return
    from laya.pipeline import agent_budget

    try:
        await agent_budget.record_rate_limit(await rate_row(login_id or DEFAULT_ID), info)
    except Exception as e:
        log.warning("codex_login_record_failed", login=login_id, error=str(e))
