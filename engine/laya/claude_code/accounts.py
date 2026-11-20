# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Claude accounts: several Claude subscriptions as sources of usage quota.

The **default account** (``DEFAULT_ID``) is whatever the Claude Code CLI is
logged in to (no ``CLAUDE_CONFIG_DIR`` override). It has no row and serves
Laya only while no account has been added. Every **added account** is a separate Claude Code configuration
directory under ``~/.laya/claude-accounts/<id>/`` (mode 0700). Laya runs
``claude`` with ``CLAUDE_CONFIG_DIR=<dir>`` for it, so its login, its MCP
servers and their OAuth tokens stay in the CLI's own storage; this module
never reads or stores a token.

Parts:

- registry: the module-owned table ``claude_code_accounts`` (created with
  ``CREATE TABLE IF NOT EXISTS``), the email / plan / organization id from
  ``claude auth status --json`` and whether the account is eligible
  (logged in, its directory exists, not the same Claude organization as an
  earlier added account). Once an account has been added, Laya uses only
  added accounts; the CLI's own login (the default account) is used only
  while no account has been added, so a ``/login`` in a Claude Code session
  never changes what Laya runs on;
- routing: :func:`pick` chooses the account for an inference stage or a chat
  turn (``llm/agent_backend.py``), sticky with hysteresis and a minimum
  dwell, and :func:`failover` the next account after a rate-limit
  rejection. :func:`pick_for_platform` chooses the account for a collector,
  My work, an action or a send turn: only an account whose connector check
  shows the platform connected and whose own tool listing has the
  platform's required tools (hard gating; never failed over);
- usage: :func:`record` stores each turn's ``rate_limit_info`` in
  ``agent_rate_limit_state`` under ``claude_code@<id>`` (an added account) or
  ``claude_code@org:<orgId>`` (the CLI login, keyed by the organization it
  is logged in to); :func:`poll_usage` reads ``claude -p "/usage"`` (no
  model call) every ``USAGE_POLL_SECONDS``; :func:`native_block` tells the
  agent budget whether every account is over its threshold;
- sign-in: :func:`start_login` runs ``claude auth login --claudeai`` (or
  ``claude mcp login <server> --no-browser`` for a connector) in a PTY, shows
  the authorize URL the CLI prints and writes the code the user pastes to
  the CLI. One sign-in runs at a time and ends after ``LOGIN_TIMEOUT``
  seconds. The pasted code is never logged or returned;
- connectors: :func:`check_connectors` lists the MCP servers each account's
  headless session gets (the CLI's ``system/init`` message, no model call)
  as a matrix of Slack, Gmail, Google Drive, Linear, Notion and GitHub.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from laya.claude_code import store
from laya.config import LAYA_HOME, get_agent_binary
from laya.db.sqlite import get_db

log = structlog.get_logger()

DEFAULT_ID = "default"
AGENT_ID = "claude_code"

ACCOUNTS_DIR = LAYA_HOME / "claude-accounts"

# Routing: an account is left once a window's utilization reaches its pause
# threshold (agent_budget pause_at_utilization), and a preferred account is
# returned to only once every window is HYSTERESIS below it. A voluntary
# switch needs the current account to have been in use for DWELL_SECONDS.
HYSTERESIS = 0.10
DWELL_SECONDS = 600.0
# Threshold for a window type agent_budget has no setting for.
OTHER_WINDOW_THRESHOLD = 0.9
# A rejection the CLI reported without a reset time blocks the account this long.
REJECTION_HOLD_SECONDS = 1800
# Usage of every account is read with `claude -p "/usage"` this often.
USAGE_POLL_SECONDS = 600.0
USAGE_TIMEOUT = 90.0

LOGIN_TIMEOUT = 600.0
POLL_SECONDS = 0.2
STATUS_CACHE_SECONDS = 60.0
OUTPUT_LIMIT = 64 * 1024

CONNECTORS_URL = "https://claude.ai/customize/connectors"

_TABLE = """CREATE TABLE IF NOT EXISTS claude_code_accounts (
    id                TEXT PRIMARY KEY,
    config_dir        TEXT NOT NULL,
    email             TEXT,
    plan              TEXT,
    org_name          TEXT,
    logged_in         INTEGER NOT NULL DEFAULT 0,
    status_checked_at TEXT,
    created_at        TEXT NOT NULL,
    org_id            TEXT
)"""

# Rows of agent_rate_limit_state that keyed the CLI login by slot rather than
# by organization; their state may belong to whichever account the CLI was
# logged in to before, so they are dropped.
_LEGACY_RATE_ROWS = ("claude_code", "claude_code@default")

# claude_code_state platform for the connector matrices (key connectors:<id>).
_STATE_PLATFORM = "accounts"


class AccountError(Exception):
    """A request this module refuses; ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


# ── table ────────────────────────────────────────────────────────────────

async def _db():
    db = await get_db()
    # The mark lives on the connection object: an id() of an earlier, closed
    # connection can be reused by a new one, which would then lack the table.
    if not getattr(db, "_laya_claude_accounts_table", False):
        await store.ensure_tables(db)
        await db.execute(_TABLE)
        # The table belongs to this module (no numbered migration); a table
        # created before the org_id column existed gets it here.
        cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(claude_code_accounts)")}
        if "org_id" not in cols:
            await db.execute("ALTER TABLE claude_code_accounts ADD COLUMN org_id TEXT")
        with contextlib.suppress(Exception):
            await db.execute(
                f"DELETE FROM agent_rate_limit_state WHERE agent_id IN ({','.join('?' * len(_LEGACY_RATE_ROWS))})",
                _LEGACY_RATE_ROWS,
            )
        await db.commit()
        db._laya_claude_accounts_table = True
    return db


def agent_row(account_id: str, org_id: str | None = None) -> str | None:
    """The ``agent_rate_limit_state`` row of an account.

    An added account has its own row. The CLI login (the default account) is
    keyed by the organization it is logged in to, so a ``/login`` to another
    account starts from a clean row; without a known organization it has none.
    """
    if account_id != DEFAULT_ID:
        return f"{AGENT_ID}@{account_id}"
    return f"{AGENT_ID}@org:{org_id}" if org_id else None


async def rate_row(account_id: str) -> str | None:
    """:func:`agent_row` with the CLI login's current organization."""
    if account_id != DEFAULT_ID:
        return agent_row(account_id)
    return agent_row(DEFAULT_ID, (await default_status()).get("org_id"))


async def list_rows() -> list[dict[str, Any]]:
    db = await _db()
    rows = await db.execute_fetchall("SELECT * FROM claude_code_accounts ORDER BY created_at, id")
    return [dict(r) for r in rows]


async def get_row(account_id: str) -> dict[str, Any] | None:
    db = await _db()
    rows = await db.execute_fetchall("SELECT * FROM claude_code_accounts WHERE id = ?", (account_id,))
    return dict(rows[0]) if rows else None


async def _update_row(account_id: str, **fields: Any) -> None:
    db = await _db()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE claude_code_accounts SET {assignments} WHERE id = ?", (*fields.values(), account_id)
    )
    await db.commit()


def account_env(row: dict[str, Any] | None) -> dict[str, str]:
    """Environment overrides that make ``claude`` use an account (none for the default)."""
    if not row:
        return {}
    return {"CLAUDE_CONFIG_DIR": row["config_dir"]}


async def env_for(account_id: str) -> dict[str, str]:
    if account_id == DEFAULT_ID:
        return {}
    row = await get_row(account_id)
    if row is None:
        raise AccountError("Account not found", status=404)
    return account_env(row)


def _new_account_dir(account_id: str) -> Path:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(ACCOUNTS_DIR, 0o700)
    path = ACCOUNTS_DIR / account_id
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _remove_account_dir(path: str) -> None:
    """Delete an account directory, only when it lies inside ACCOUNTS_DIR."""
    p = Path(path).resolve()
    base = ACCOUNTS_DIR.resolve()
    if p.parent != base:
        log.warning("claude_account_dir_outside_base", path=str(p))
        return
    shutil.rmtree(p, ignore_errors=True)


# ── CLI helpers ──────────────────────────────────────────────────────────


def _cli_env(extra: dict[str, str] | None) -> dict[str, str]:
    from laya.claude_code.terminal import _PARENT_SESSION_MARKERS
    from laya.config import _augmented_path

    env = {**os.environ, "PATH": _augmented_path()}
    for name in _PARENT_SESSION_MARKERS:
        env.pop(name, None)
    env.update(extra or {})
    return env


async def _run_cli(
    args: list[str], env: dict[str, str] | None = None, timeout: float = 60.0, cwd: str | None = None,
) -> tuple[int, str, str]:
    """Run a short ``claude`` subcommand; returns (exit code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_cli_env(env),
            cwd=cwd,
        )
    except OSError as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return 124, "", f"{args[1] if len(args) > 1 else args[0]} timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def parse_auth_status(text: str) -> dict[str, Any]:
    """``claude auth status --json`` → {logged_in, email, plan, org_name}."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "logged_in": bool(data.get("loggedIn")),
        "email": data.get("email") or None,
        "plan": data.get("subscriptionType") or None,
        "org_name": data.get("orgName") or None,
        "org_id": data.get("orgId") or None,
    }


async def auth_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    _, out, _ = await _run_cli([get_agent_binary("claude_code"), "auth", "status", "--json"], env, timeout=30)
    return parse_auth_status(out)


_default_status: tuple[float, dict[str, Any]] | None = None


async def default_status(force: bool = False) -> dict[str, Any]:
    """Auth status of the default login, cached for STATUS_CACHE_SECONDS."""
    global _default_status
    if not force and _default_status and time.monotonic() - _default_status[0] < STATUS_CACHE_SECONDS:
        return _default_status[1]
    status = await auth_status()
    _default_status = (time.monotonic(), status)
    return status


async def refresh_status(account_id: str) -> dict[str, Any]:
    """Read an added account's auth status and store it."""
    row = await get_row(account_id)
    if row is None:
        raise AccountError("Account not found", status=404)
    status = await auth_status(account_env(row))
    await _store_status(row, status)
    return status


async def _store_status(row: dict[str, Any], status: dict[str, Any]) -> None:
    """Store an added account's auth status.

    When the directory is now logged in to another Claude organization, the
    usage recorded for the old one no longer applies and is dropped.
    """
    account_id = row["id"]
    logged_in = bool(status.get("logged_in"))
    new_org = status.get("org_id") if logged_in else None
    if new_org and row.get("org_id") and new_org != row["org_id"]:
        await _clear_usage(account_id)
        log.info("claude_account_identity_changed", account=account_id)
    await _update_row(
        account_id,
        logged_in=1 if logged_in else 0,
        email=status["email"] if logged_in else row.get("email"),
        plan=status["plan"] if logged_in else row.get("plan"),
        org_name=status["org_name"] if logged_in else row.get("org_name"),
        org_id=new_org or row.get("org_id"),
        status_checked_at=store.now_iso(),
    )


async def _clear_usage(account_id: str) -> None:
    db = await _db()
    await db.execute("DELETE FROM agent_rate_limit_state WHERE agent_id = ?", (agent_row(account_id),))
    await db.commit()
    await store.delete_state(_STATE_PLATFORM, [_usage_key(account_id)])


def _same_account(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def _identity(row: dict[str, Any]) -> str | None:
    """What makes two rows the same Claude account: the organization id, else the email."""
    if row.get("org_id"):
        return f"org:{row['org_id']}"
    email = (row.get("email") or "").strip().lower()
    return f"email:{email}" if email else None


def has_own_accounts(rows: list[dict[str, Any]]) -> bool:
    """Whether the user has added an account (one that was signed in at least once).

    A row of a first sign-in that is still running does not count, so the CLI
    login keeps serving Laya until the new account is ready.
    """
    return any(r["logged_in"] or r.get("email") for r in rows)


def own_eligible(rows: list[dict[str, Any]]) -> list[str]:
    """The added accounts routing may use, in creation order: logged in,
    directory present, and not the same Claude account as an earlier row."""
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not row["logged_in"] or not Path(row["config_dir"]).is_dir():
            continue
        ident = _identity(row)
        if ident and ident in seen:
            continue
        if ident:
            seen.add(ident)
        ids.append(row["id"])
    return ids


async def eligible_ids() -> list[str]:
    """Accounts routing may use, in preference order.

    The added accounts that are eligible; the CLI login (``DEFAULT_ID``) only
    while no account has been added. Empty when accounts were added but none
    can be used (e.g. all signed out): Laya then does not fall back to
    whatever the CLI happens to be logged in to.
    """
    rows = await list_rows()
    if not has_own_accounts(rows):
        return [DEFAULT_ID]
    return own_eligible(rows)


class NoEligibleAccount(AccountError):
    """Accounts were added, but none of them can be used now."""

    def __init__(self, message: str = "No Claude account Laya can use. Sign in again in Settings → Claude accounts.") -> None:
        super().__init__(message, status=409)


# ── usage ────────────────────────────────────────────────────────────────


def _thresholds() -> dict[str, float]:
    from laya.pipeline.agent_budget import DEFAULT_PAUSE_AT_UTILIZATION, get_agent_budget_config

    try:
        return dict(get_agent_budget_config().get("pause_at_utilization") or DEFAULT_PAUSE_AT_UTILIZATION)
    except Exception:
        return dict(DEFAULT_PAUSE_AT_UTILIZATION)


async def windows_of(account_id: str, now: float | None = None) -> dict[str, dict[str, Any]]:
    """The account's current usage windows (see agent_budget.rate_limit_windows),
    with the percentages of the last ``/usage`` poll merged in (:func:`merge_usage`)."""
    from laya.pipeline.agent_budget import get_rate_limit_raw, rate_limit_windows

    row = await rate_row(account_id)
    raw = (await get_rate_limit_raw(row)) if row else None
    windows = rate_limit_windows(raw or {}, now=now)
    updated = await _rate_updated_epoch(row) if row else None
    return merge_usage(windows, await stored_usage(account_id), updated, now)


def merge_usage(
    windows: dict[str, dict[str, Any]],
    polled: dict[str, Any] | None,
    rate_updated: float | None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Combine the rate-limit signal of the last turn with the last ``/usage`` poll.

    - The utilization of ``five_hour`` / ``seven_day`` comes from whichever is
      newer; a window only one of them knows is taken from it.
    - Status (``rejected``) and the epoch ``resets_at`` stay with the
      rate-limit signal; a poll older than a window's reset no longer counts.
    - The poll's human reset text is added as ``resets_text`` for display.
    """
    now = time.time() if now is None else now
    out = {k: dict(v) for k, v in windows.items()}
    if not polled:
        return out
    at = float(polled.get("at") or 0)
    for name, p in (polled.get("windows") or {}).items():
        if not isinstance(p, dict) or p.get("utilization") is None:
            continue
        w = out.setdefault(name, {"utilization": None, "resets_at": None, "status": None})
        try:
            reset = float(w["resets_at"]) if w.get("resets_at") else None
        except (TypeError, ValueError):
            reset = None
        if reset is not None and reset <= now and at < reset:
            continue
        if w.get("utilization") is None or rate_updated is None or at >= rate_updated:
            w["utilization"] = p["utilization"]
        if p.get("resets"):
            w["resets_text"] = p["resets"]
    return out


async def _rate_updated_epoch(row: str) -> float | None:
    from datetime import datetime, timezone

    db = await _db()
    rows = await db.execute_fetchall("SELECT updated_at FROM agent_rate_limit_state WHERE agent_id = ?", (row,))
    if not rows or not rows[0]["updated_at"]:
        return None
    try:
        dt = datetime.fromisoformat(str(rows[0]["updated_at"]).replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def window_over(name: str, w: dict[str, Any], thresholds: dict[str, float], margin: float = 0.0) -> bool:
    """Whether one window is at (or within ``margin`` of) its threshold.

    A rejection always counts. A warning with an unknown utilization counts
    (only for margin 0, the switch-away test), like the agent budget's own
    native check.
    """
    if w.get("status") == "rejected":
        return True
    util = w.get("utilization")
    if util is None:
        return margin == 0.0 and w.get("status") == "allowed_warning"
    return float(util) >= thresholds.get(name, OTHER_WINDOW_THRESHOLD) - margin


def account_over(windows: dict[str, dict], thresholds: dict[str, float], margin: float = 0.0) -> bool:
    return any(window_over(n, w, thresholds, margin) for n, w in windows.items())


def headroom(windows: dict[str, dict], thresholds: dict[str, float]) -> float:
    """Smallest distance of any window to its threshold (unknown usage = full room)."""
    room = 1.0
    for name, w in windows.items():
        thr = thresholds.get(name, OTHER_WINDOW_THRESHOLD)
        if w.get("status") == "rejected":
            return -1.0
        util = w.get("utilization")
        room = min(room, thr - (float(util) if util is not None else 0.0))
    return room


def _rejected(windows: dict[str, dict]) -> bool:
    return any(w.get("status") == "rejected" for w in windows.values())


# ── routing ──────────────────────────────────────────────────────────────


@dataclass
class _Route:
    current: str | None = None
    since: float = 0.0


_route = _Route()


def choose(
    order: list[str],
    windows: dict[str, dict[str, dict]],
    thresholds: dict[str, float],
    current: str | None,
    since: float,
    now: float,
) -> str:
    """The account to use next.

    - No current account (or it is no longer eligible): the first account in
      ``order`` that is not over its threshold, else the one with most headroom.
    - The current account is over its threshold: after ``DWELL_SECONDS`` (at
      once when it was rejected) the account with most headroom that is not
      over; while every account is over, the current one stays.
    - The current account has room: a preferred account (earlier in
      ``order``) is returned to after ``DWELL_SECONDS`` once all its windows
      are ``HYSTERESIS`` below their thresholds.
    """
    def over(a: str, margin: float = 0.0) -> bool:
        return account_over(windows.get(a, {}), thresholds, margin)

    def best(candidates: list[str]) -> str | None:
        free = [a for a in candidates if not over(a)]
        if not free:
            return None
        return max(free, key=lambda a: (headroom(windows.get(a, {}), thresholds), -order.index(a)))

    if current not in order:
        first_free = next((a for a in order if not over(a)), None)
        return first_free or max(order, key=lambda a: headroom(windows.get(a, {}), thresholds))

    dwelled = now - since >= DWELL_SECONDS
    if over(current):
        if not dwelled and not _rejected(windows.get(current, {})):
            return current
        return best([a for a in order if a != current]) or current

    if dwelled:
        for a in order[: order.index(current)]:
            if not over(a, HYSTERESIS):
                return a
    return current


async def _decide(commit: bool, order: list[str] | None = None) -> str:
    order = await eligible_ids() if order is None else order
    if not order:
        raise NoEligibleAccount()
    if len(order) == 1:
        if commit and _route.current != order[0]:
            _route.current, _route.since = order[0], time.time()
        return order[0]
    now = time.time()
    windows = {a: await windows_of(a, now) for a in order}
    chosen = choose(order, windows, _thresholds(), _route.current, _route.since, now)
    if commit and chosen != _route.current:
        log.info("claude_account_switch", previous=_route.current, account=chosen)
        _route.current, _route.since = chosen, now
    return chosen


async def _env_of(account_id: str) -> dict[str, str]:
    return {} if account_id == DEFAULT_ID else account_env(await get_row(account_id))


async def _fallback_account(exclude: str | None = None) -> str | None:
    """The account to use when routing itself failed: the first eligible added
    account, or the CLI login only when no account was added."""
    rows = await list_rows()
    if not has_own_accounts(rows):
        return DEFAULT_ID if exclude != DEFAULT_ID else None
    return next((a for a in own_eligible(rows) if a != exclude), None)


async def pick(purpose: str = "inference") -> tuple[str, dict[str, str]]:
    """The account for one inference stage or chat turn and its environment.

    Raises :class:`NoEligibleAccount` when accounts were added but none can be
    used. Any other routing failure falls back to the first eligible added
    account (the CLI login only when no account was added, or when the
    account registry itself cannot be read).
    """
    try:
        account_id = await _decide(commit=True)
        return account_id, await _env_of(account_id)
    except NoEligibleAccount:
        raise
    except Exception as e:
        log.warning("claude_account_pick_failed", purpose=purpose, error=str(e))
        try:
            fallback = await _fallback_account()
            env = await _env_of(fallback) if fallback else {}
        except Exception as e2:
            # Without the account registry no added account's directory is
            # known, so the turn can only run on the CLI login.
            log.warning("claude_account_registry_unavailable", purpose=purpose, error=str(e2))
            return DEFAULT_ID, {}
        if fallback is None:
            raise NoEligibleAccount() from e
        return fallback, env


async def current_account() -> str | None:
    """The account processing uses now (what the next :func:`pick` returns)."""
    try:
        return await _decide(commit=False)
    except Exception:
        return None


async def failover(failed: str) -> tuple[str, dict[str, str]] | None:
    """The account to retry on after ``failed`` was rejected, or None.

    The retry account becomes the current one (no dwell: the failed account
    cannot run).
    """
    try:
        order = [a for a in await eligible_ids() if a != failed]
        if not order:
            return None
        now = time.time()
        thresholds = _thresholds()
        windows = {a: await windows_of(a, now) for a in order}
        free = [a for a in order if not account_over(windows[a], thresholds)]
        if not free:
            return None
        nxt = max(free, key=lambda a: (headroom(windows[a], thresholds), -order.index(a)))
    except Exception as e:
        log.warning("claude_account_failover_failed", error=str(e))
        try:
            nxt = await _fallback_account(exclude=failed)
        except Exception:
            return None
        if nxt is None:
            return None
    log.info("claude_account_failover", failed=failed, account=nxt)
    _route.current, _route.since = nxt, time.time()
    return nxt, await _env_of(nxt)


# ── per-platform gating ──────────────────────────────────────────────────


@dataclass
class Assignment:
    """The account a platform's turn runs on, with that account's own tool names."""

    account_id: str
    env: dict[str, str]
    # role -> tool name of the platform on this account
    tools: dict[str, str]
    # every MCP tool this account's session has (a turn denies all but its own)
    all_tools: list[str]
    # role -> tool name of every platform available on this account
    platform_tools: dict[str, dict[str, str]] = field(default_factory=dict)

    def source_fields(self) -> dict[str, Any]:
        """What a collector's ``source`` carries so every turn of the sweep
        (collector, re-check, linked documents) runs on this account."""
        return {
            "tools": dict(self.tools),
            "deny_tools": list(self.all_tools),
            "account_id": self.account_id,
            "account_env": dict(self.env),
            "account_platform_tools": {p: dict(t) for p, t in self.platform_tools.items()},
        }


async def platform_assignment(
    account_id: str, platform: str, roles: tuple[str, ...] = (), force: bool = False,
) -> tuple[Assignment | None, str | None]:
    """(the assignment, None) when ``account_id`` can run ``platform``, else (None, reason).

    An added account needs its connector check to show the platform
    connected; every account needs its own tool listing to have the
    platform's required tools (and ``roles``).
    """
    from laya.claude_code import discover

    if account_id != DEFAULT_ID:
        matrix = await stored_connectors(account_id)
        state = (((matrix or {}).get("connectors") or {}).get(platform) or {}).get("state")
        if state != "connected":
            return None, f"{platform} is not connected on this account"
    env = await _env_of(account_id)
    listing = await discover.account_listing(account_id, env, force=force)
    pd = listing.platforms.get(platform)
    if pd is None or not pd.available:
        return None, (pd.problem if pd else None) or f"{platform} has no tools on this account"
    missing = [r for r in roles if not pd.tools.get(r)]
    if missing:
        return None, f"{platform} lacks {', '.join(missing)} on this account"
    return Assignment(
        account_id=account_id, env=env, tools=dict(pd.tools), all_tools=list(listing.all_tools),
        platform_tools={p: dict(d.tools) for p, d in listing.platforms.items() if d.available},
    ), None


async def pick_for_platform(platform: str, roles: tuple[str, ...] = ()) -> Assignment | None:
    """The account a collector, My work, action or send turn of ``platform`` runs on.

    Only accounts that pass :func:`platform_assignment`. Among them the
    account inference routing uses now when it has room, else the one with
    most headroom that is under its thresholds, else the first. None when no
    account can run the platform. Never fails over: a turn that changes
    something must not run twice.
    """
    order = await eligible_ids()
    ready: dict[str, Assignment] = {}
    for account_id in order:
        try:
            assignment, reason = await platform_assignment(account_id, platform, roles)
        except Exception as e:
            assignment, reason = None, str(e)
        if assignment is not None:
            ready[account_id] = assignment
        else:
            log.debug("claude_account_not_eligible", account=account_id, platform=platform, reason=reason)
    if not ready:
        return None
    candidates = [a for a in order if a in ready]
    if len(candidates) == 1:
        return ready[candidates[0]]
    now = time.time()
    thresholds = _thresholds()
    windows = {a: await windows_of(a, now) for a in candidates}
    free = [a for a in candidates if not account_over(windows[a], thresholds)]
    if _route.current in free:
        return ready[_route.current]
    if free:
        return ready[max(free, key=lambda a: (headroom(windows[a], thresholds), -candidates.index(a)))]
    return ready[candidates[0]]


_REJECTION_RE = re.compile(
    r"usage limit|hit your (?:usage )?limit|rate[ _-]?limit(?:ed)? (?:reached|exceeded)|limit reached",
    re.IGNORECASE,
)


def is_rejection(rate_limit_info: dict | None, error: str | None = None) -> bool:
    """Whether a turn failed because the account's usage limit was hit."""
    if isinstance(rate_limit_info, dict) and rate_limit_info.get("status") == "rejected":
        return True
    return bool(error and _REJECTION_RE.search(error))


async def record(account_id: str | None, rate_limit_info: dict | None, rejected: bool = False) -> None:
    """Store a turn's rate-limit signal under the account's row. Never raises.

    A rejection without a signal is stored as a rejection that holds for
    ``REJECTION_HOLD_SECONDS``.
    """
    from laya.pipeline import agent_budget

    info = rate_limit_info if isinstance(rate_limit_info, dict) and rate_limit_info else None
    if rejected and (info is None or info.get("status") != "rejected"):
        info = {
            **(info or {}),
            "status": "rejected",
            "resetsAt": (info or {}).get("resetsAt") or int(time.time()) + REJECTION_HOLD_SECONDS,
        }
    if info is None:
        return
    try:
        account_id = account_id or DEFAULT_ID
        if account_id == DEFAULT_ID and rejected:
            # A rejection is filed under the organization the CLI is logged in
            # to right now, not the one cached before a possible /login.
            await default_status(force=True)
        row = await rate_row(account_id)
        if row is None:
            return
        await agent_budget.record_rate_limit(row, info)
    except Exception as e:
        log.warning("claude_account_record_failed", account=account_id, error=str(e))


async def native_block(thresholds: dict[str, float]) -> tuple[bool, str | None, str]:
    """(every eligible account over its threshold, earliest recovery as a db
    timestamp or None, reason) for the agent budget."""
    from laya.db.timeutil import db_ts_from_epoch

    order = await eligible_ids()
    if not order:
        return True, None, f"{ACCOUNTS_PAUSE_REASON} (no Claude account can be used; sign in again)"
    now = time.time()
    recoveries: list[int] = []
    parts: list[str] = []
    for a in order:
        windows = await windows_of(a, now)
        over = [n for n, w in windows.items() if window_over(n, w, thresholds)]
        if not over:
            return False, None, ""
        resets = [int(windows[n]["resets_at"]) for n in over if windows[n].get("resets_at")]
        if resets:
            recoveries.append(max(resets))
        parts.append(f"{a} {'+'.join(over)}")
    until = db_ts_from_epoch(min(recoveries)) if recoveries else None
    return True, until, f"{ACCOUNTS_PAUSE_REASON} ({', '.join(parts)})"


ACCOUNTS_PAUSE_REASON = "claude_code: every Claude account is over its usage threshold"


# ── usage poll (`claude -p "/usage"`) ────────────────────────────────────

# One line of the CLI's /usage block, e.g.
# "Current week (all models): 2% used · resets Nov 26 at 4am (Europe/Berlin)".
_USAGE_LINE_RE = re.compile(r"^(Current session|Current week \((.+?)\)): (\d+)% used(?: \u00b7 resets (.+))?$")

# Last poll per account (monotonic seconds), to space scheduled polls.
_last_poll: dict[str, float] = {}


def parse_usage(text: str) -> dict[str, Any] | None:
    """The usage block ``claude -p "/usage"`` prints, or None when the text has none.

    ``Current session`` is the ``five_hour`` window, ``Current week (all
    models)`` the ``seven_day`` one; other weekly lines (one model) are kept
    for display only. A window without a line stays unknown (never 0 %).
    """
    windows: dict[str, dict[str, Any]] = {}
    models: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _USAGE_LINE_RE.match(line.strip())
        if not m:
            continue
        percent = int(m.group(3))
        entry = {"utilization": percent / 100.0, "resets": (m.group(4) or "").strip() or None}
        if m.group(1) == "Current session":
            windows["five_hour"] = entry
        elif m.group(2).strip().lower() == "all models":
            windows["seven_day"] = entry
        else:
            models.append({"label": m.group(2).strip(), "percent": percent, "resets": entry["resets"]})
    if not windows and not models:
        return None
    return {"windows": windows, "models": models}


def _usage_key(account_id: str, org_id: str | None = None) -> str:
    if account_id != DEFAULT_ID:
        return f"usage:{account_id}"
    return f"usage:org:{org_id}" if org_id else "usage:org:unknown"


async def stored_usage(account_id: str) -> dict[str, Any] | None:
    org = (await default_status()).get("org_id") if account_id == DEFAULT_ID else None
    if account_id == DEFAULT_ID and not org:
        return None
    key = _usage_key(account_id, org)
    raw = (await store.get_state(_STATE_PLATFORM, key)).get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _fresh_turn_data(account_id: str, within: float) -> bool:
    """Whether a real turn recorded both windows' utilization for the account
    within ``within`` seconds (then a poll would add nothing)."""
    from laya.pipeline.agent_budget import get_rate_limit_raw, rate_limit_windows

    row = await rate_row(account_id)
    if row is None:
        return False
    updated = await _rate_updated_epoch(row)
    if updated is None or time.time() - updated > within:
        return False
    windows = rate_limit_windows(await get_rate_limit_raw(row) or {})
    return all((windows.get(w) or {}).get("utilization") is not None for w in ("five_hour", "seven_day"))


async def poll_usage(account_id: str, force: bool = False) -> dict[str, Any]:
    """Read one account's usage with ``claude -p "/usage"`` (no model call).

    Auth status first: a signed-out account is stored as such and not polled.
    Skipped while a sign-in runs in the account's directory, and (unless
    ``force``) when a real turn recorded both windows in the last
    ``USAGE_POLL_SECONDS``. Lines that are missing keep their last known
    value. Returns ``{polled, logged_in, error}``.
    """
    if login_running_for(account_id):
        return {"polled": False, "logged_in": None, "error": "sign-in running"}
    _last_poll[account_id] = time.monotonic()
    row = None
    if account_id == DEFAULT_ID:
        status = await default_status(force=True)
        env: dict[str, str] = {}
    else:
        row = await get_row(account_id)
        if row is None:
            raise AccountError("Account not found", status=404)
        env = account_env(row)
        status = await auth_status(env)
        await _store_status(row, status)
    if not status.get("logged_in"):
        return {"polled": False, "logged_in": False, "error": None}
    if not force and await _fresh_turn_data(account_id, USAGE_POLL_SECONDS):
        return {"polled": False, "logged_in": True, "error": None}

    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="laya-claude-usage-")
    try:
        code, out, err = await _run_cli(
            [get_agent_binary("claude_code"), "-p", "/usage", "--output-format", "json",
             "--no-session-persistence"],
            env, timeout=USAGE_TIMEOUT, cwd=tmpdir,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    try:
        data = json.loads(out or "{}")
    except ValueError:
        data = {}
    text = data.get("result") if isinstance(data, dict) else None
    parsed = parse_usage(text if isinstance(text, str) else "")
    if parsed is None:
        error = (err or (text if isinstance(text, str) else "") or f"exit {code}").strip()[:200]
        log.info("claude_account_usage_unknown", account=account_id, error=error)
        return {"polled": False, "logged_in": True, "error": error or "no usage block"}

    previous = await stored_usage(account_id) or {}
    windows = {**(previous.get("windows") or {}), **parsed["windows"]}
    value = {"windows": windows, "models": parsed["models"], "at": time.time(), "as_of": store.now_iso()}
    await store.set_state(_STATE_PLATFORM, _usage_key(account_id, status.get("org_id")), json.dumps(value))
    return {"polled": True, "logged_in": True, "error": None}


def _poll_targets(rows: list[dict[str, Any]]) -> list[str]:
    targets = [DEFAULT_ID]
    targets += [r["id"] for r in rows if r["logged_in"] or r.get("email")]
    return targets


async def poll_due_usage() -> list[str]:
    """Poll every account whose last poll is ``USAGE_POLL_SECONDS`` old (service loop)."""
    polled: list[str] = []
    rows = await list_rows()
    if not rows and not await store.list_sources():
        # Claude Code is not used by Laya at all (no account, no platform).
        return polled
    now = time.monotonic()
    for account_id in _poll_targets(rows):
        last = _last_poll.get(account_id)
        if last is not None and now - last < USAGE_POLL_SECONDS:
            continue
        try:
            result = await poll_usage(account_id)
        except Exception as e:
            log.warning("claude_account_usage_poll_failed", account=account_id, error=str(e))
            continue
        if result.get("polled"):
            polled.append(account_id)
    return polled


async def poll_all_usage() -> None:
    """Poll every account now ("Refresh")."""
    targets = _poll_targets(await list_rows())

    async def one(account_id: str) -> None:
        try:
            await poll_usage(account_id, force=True)
        except Exception as e:
            log.warning("claude_account_usage_poll_failed", account=account_id, error=str(e))

    await asyncio.gather(*(one(a) for a in targets))


# ── PTY sign-in ──────────────────────────────────────────────────────────


class PtyProcess:
    """A CLI command in a pseudo-terminal whose output is collected."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: str) -> None:
        from laya.claude_code.terminal import _make_controlling_tty, _set_winsize

        self.output = bytearray()
        self.returncode: int | None = None
        master, slave = pty.openpty()
        try:
            # Wide, so the CLI prints the authorize URL on one line.
            _set_winsize(slave, 40, 1000)
            self.proc = subprocess.Popen(
                args, stdin=slave, stdout=slave, stderr=slave, cwd=cwd,
                env={**env, "TERM": "xterm-256color"},
                start_new_session=True, preexec_fn=_make_controlling_tty, close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.master: int | None = master
        asyncio.get_running_loop().add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        if self.master is None:
            return
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self.output += data
            if len(self.output) > OUTPUT_LIMIT:
                del self.output[: len(self.output) - OUTPUT_LIMIT]
            return
        self._detach()
        asyncio.get_running_loop().create_task(self._reap())

    def _detach(self) -> None:
        if self.master is None:
            return
        asyncio.get_running_loop().remove_reader(self.master)
        with contextlib.suppress(OSError):
            os.close(self.master)
        self.master = None

    async def _reap(self) -> None:
        self.returncode = await asyncio.to_thread(self.proc.wait)

    def write(self, data: bytes) -> None:
        if self.master is None:
            raise AccountError("The sign-in has ended", status=409)
        os.write(self.master, data)

    async def terminate(self) -> None:
        if self.proc.poll() is None:
            for sig in (signal.SIGHUP, signal.SIGTERM):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, sig)
            try:
                await asyncio.wait_for(asyncio.to_thread(self.proc.wait), 2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                await asyncio.to_thread(self.proc.wait)
        self._detach()
        if self.returncode is None:
            self.returncode = self.proc.returncode


def _start_pty(args: list[str], env: dict[str, str], cwd: str) -> PtyProcess:
    return PtyProcess(args, _cli_env(env), cwd)


# OSC 8 hyperlink opener: ESC ] 8 ; <params> ; <target> (BEL | ESC \). The
# CLI prints the authorize URL as such a link, often with colour codes around
# (or inside) the visible text.
_OSC8_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b[()][0-9A-Za-z]|\x1b[=>78DEHMNOZc]")
_ANSI_RE = re.compile(f"{_OSC_RE.pattern}|{_CSI_RE.pattern}|{_ESC_RE.pattern}")
_URL_RE = re.compile(r"https://[^\s\x07\x1b'\"<>]+")


def strip_escapes(text: str) -> str:
    """Terminal text without escape sequences; an OSC 8 link keeps its target
    (followed by a space) so it reads as a plain URL."""
    text = _OSC8_RE.sub(lambda m: f"{m.group(1)} " if m.group(1) else "", text)
    return _ANSI_RE.sub("", text)


def parse_login_url(text: str) -> str | None:
    """The authorize URL a ``claude auth login`` / ``claude mcp login`` printed:
    the first https URL once escape sequences are removed."""
    m = _URL_RE.search(strip_escapes(text))
    return m.group(0).rstrip(".,;:") if m else None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in strip_escapes(text).replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln and not ln.startswith("Paste code")]
    return lines[-1][:300] if lines else ""


@dataclass
class LoginFlow:
    id: str
    kind: str  # "account" | "connector"
    account_id: str
    new_account: bool
    server: str | None = None
    platform: str | None = None
    state: str = "starting"  # starting | waiting_code | verifying | done | failed | cancelled | timed_out
    url: str | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=store.now_iso)
    proc: Any = None
    task: asyncio.Task | None = None
    # The pasted code, kept only to remove it from CLI output that is shown.
    secret: str | None = None

    @property
    def active(self) -> bool:
        return self.state in ("starting", "waiting_code", "verifying")

    def redact(self, text: str) -> str:
        return text.replace(self.secret, "[code]") if self.secret else text

    @property
    def expires_at(self) -> str:
        from datetime import datetime, timedelta

        try:
            start = datetime.fromisoformat(self.started_at)
        except ValueError:
            return self.started_at
        return (start + timedelta(seconds=LOGIN_TIMEOUT)).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "account_id": self.account_id,
            "new_account": self.new_account,
            "server": self.server, "platform": self.platform, "state": self.state,
            "url": self.url, "error": self.error, "started_at": self.started_at,
            "expires_at": self.expires_at,
        }


_flow: LoginFlow | None = None


def current_flow() -> LoginFlow | None:
    return _flow


async def _start_flow(flow: LoginFlow, args: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
    global _flow
    try:
        flow.proc = _start_pty(args, env, cwd)
    except (OSError, subprocess.SubprocessError) as e:
        raise AccountError(f"Could not start claude: {e}", status=500) from e
    _flow = flow
    flow.task = asyncio.create_task(_drive(flow), name=f"claude_login_{flow.id}")
    log.info("claude_login_started", kind=flow.kind, account=flow.account_id, server=flow.server)
    return flow.to_dict()


def _ensure_idle() -> None:
    if _flow is not None and _flow.active:
        raise AccountError("Another sign-in is in progress. Finish or cancel it first.", status=409)


async def start_login(account_id: str | None = None) -> dict[str, Any]:
    """Sign in a new account (``account_id`` None) or sign in an added one again."""
    _ensure_idle()
    if account_id is None:
        account_id = uuid.uuid4().hex[:10]
        path = _new_account_dir(account_id)
        db = await _db()
        await db.execute(
            "INSERT INTO claude_code_accounts (id, config_dir, created_at) VALUES (?, ?, ?)",
            (account_id, str(path), store.now_iso()),
        )
        await db.commit()
        new = True
    else:
        if account_id == DEFAULT_ID:
            raise AccountError("The Claude Code login is managed in Claude Code itself.")
        row = await get_row(account_id)
        if row is None:
            raise AccountError("Account not found", status=404)
        path = Path(row["config_dir"])
        new = False
    flow = LoginFlow(id=uuid.uuid4().hex, kind="account", account_id=account_id, new_account=new)
    try:
        return await _start_flow(
            flow, [get_agent_binary("claude_code"), "auth", "login", "--claudeai"],
            {"CLAUDE_CONFIG_DIR": str(path)}, str(path),
        )
    except AccountError:
        if new:
            await _forget_account(account_id, str(path))
        raise


async def submit_code(account_id: str, code: str) -> dict[str, Any]:
    """Write the code (or redirect URL) the user pasted to the account's waiting sign-in."""
    flow = _get_flow(account_id)
    if flow.state != "waiting_code":
        raise AccountError("The sign-in is not waiting for a code", status=409)
    clean = (code or "").strip()
    if not clean or len(clean) > 4096 or re.search(r"[\x00-\x1f\x7f]", clean):
        raise AccountError("Paste the code exactly as shown on the sign-in page")
    flow.secret = clean
    flow.state = "verifying"
    # The CLI reads the line in raw mode, where Enter is a carriage return.
    flow.proc.write(clean.encode() + b"\r")
    log.info("claude_login_code_submitted", kind=flow.kind, account=flow.account_id)
    return flow.to_dict()


def _get_flow(account_id: str) -> LoginFlow:
    """The latest sign-in of an account (the account's own or one of its connectors)."""
    if _flow is None or _flow.account_id != account_id:
        raise AccountError("Sign-in not found", status=404)
    return _flow


def flow_status(account_id: str) -> dict[str, Any] | None:
    return _flow.to_dict() if _flow is not None and _flow.account_id == account_id else None


def login_state() -> dict[str, Any] | None:
    """The latest sign-in (running or ended), whichever account it is for.

    The UI renders the sign-in panel from this on every poll, so a reload,
    a list refresh or navigating away never loses it.
    """
    return _flow.to_dict() if _flow is not None else None


def login_running_for(account_id: str) -> bool:
    return _flow is not None and _flow.active and _flow.account_id == account_id


async def cancel(account_id: str) -> dict[str, Any]:
    flow = _get_flow(account_id)
    if flow.active:
        await _end(flow, "cancelled", None)
    return flow.to_dict()


async def _drive(flow: LoginFlow) -> None:
    """Follow the CLI until it exits or the sign-in times out."""
    try:
        while True:
            if time.monotonic() - flow.started > LOGIN_TIMEOUT:
                await _end(flow, "timed_out", "The sign-in took longer than 10 minutes. Start it again.")
                return
            text = bytes(flow.proc.output).decode(errors="replace")
            if flow.url is None:
                url = parse_login_url(text)
                if url:
                    flow.url = url
                    if flow.state == "starting":
                        flow.state = "waiting_code"
            if flow.proc.returncode is not None:
                await _finish(flow, flow.proc.returncode, text)
                return
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("claude_login_error", kind=flow.kind, error=str(e))
        await _end(flow, "failed", "The sign-in failed unexpectedly.")


async def _finish(flow: LoginFlow, code: int, text: str) -> None:
    if flow.kind == "connector":
        if code == 0:
            await _end(flow, "done", None)
            with contextlib.suppress(Exception):
                await check_connectors(flow.account_id)
        else:
            await _end(flow, "failed", flow.redact(_last_line(text)) or "The connector sign-in failed.")
        return

    row = await get_row(flow.account_id)
    status = await auth_status(account_env(row)) if row else {"logged_in": False}
    if code != 0 or not status["logged_in"]:
        await _end(flow, "failed", flow.redact(_last_line(text)) or "The sign-in failed.")
        return
    others = [r for r in await list_rows() if r["id"] != flow.account_id and r["logged_in"]]
    mine = _identity({"org_id": status.get("org_id"), "email": status.get("email")})
    if any(
        (mine and _identity(r) == mine) or _same_account(status["email"], r.get("email")) for r in others
    ):
        # Usually the browser was still signed in to an account Laya already has.
        await _run_cli([get_agent_binary("claude_code"), "auth", "logout"], account_env(row))
        await _end(
            flow, "failed",
            f"{status['email']} is already one of your Claude accounts. Sign in to the other "
            "account on claude.ai in your browser (or use a private window) and add it again.",
        )
        return
    await _store_status(row, status)
    await _end(flow, "done", None)
    await _after_account_added()
    with contextlib.suppress(Exception):
        await check_connectors(flow.account_id)
    with contextlib.suppress(Exception):
        await poll_usage(flow.account_id, force=True)


async def _after_account_added() -> None:
    """A pause because every account was over its threshold can end now."""
    try:
        from laya.pipeline.agent_budget import maybe_resume_agent_budget

        await maybe_resume_agent_budget()
    except Exception as e:
        log.warning("claude_account_resume_check_failed", error=str(e))


async def _end(flow: LoginFlow, state: str, error: str | None) -> None:
    flow.state = state
    flow.error = error
    flow.secret = None
    if flow.proc is not None:
        with contextlib.suppress(Exception):
            await flow.proc.terminate()
    if flow.task is not None and flow.task is not asyncio.current_task() and not flow.task.done():
        flow.task.cancel()
    if flow.kind == "account" and flow.new_account and state != "done":
        row = await get_row(flow.account_id)
        if row and not row["logged_in"]:
            await _forget_account(flow.account_id, row["config_dir"])
    log.info("claude_login_ended", kind=flow.kind, account=flow.account_id, state=state)


async def _forget_account(account_id: str, config_dir: str) -> None:
    db = await _db()
    await db.execute("DELETE FROM claude_code_accounts WHERE id = ?", (account_id,))
    await db.execute("DELETE FROM agent_rate_limit_state WHERE agent_id = ?", (agent_row(account_id),))
    await db.commit()
    await store.delete_state(_STATE_PLATFORM, [f"connectors:{account_id}", _usage_key(account_id)])
    _remove_account_dir(config_dir)
    from laya.claude_code import discover

    discover.reset_account_cache(account_id)
    _last_poll.pop(account_id, None)
    if _route.current == account_id:
        _route.current = None


def user_mcp_servers(config_dir: str) -> dict[str, dict[str, Any]]:
    """The user-scope MCP servers in a configuration directory's ``.claude.json``."""
    try:
        data = json.loads((Path(config_dir) / ".claude.json").read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


async def remove(account_id: str) -> None:
    """Sign out of every MCP server and the account, then delete its directory."""
    if account_id == DEFAULT_ID:
        raise AccountError("The Claude Code login is managed in Claude Code itself and cannot be removed here.")
    row = await get_row(account_id)
    if row is None:
        raise AccountError("Account not found", status=404)
    if _flow is not None and _flow.active and _flow.account_id == account_id:
        await _end(_flow, "cancelled", None)
    binary = get_agent_binary("claude_code")
    env = account_env(row)
    for server in user_mcp_servers(row["config_dir"]):
        code, _, err = await _run_cli([binary, "mcp", "logout", server], env)
        if code != 0:
            log.info("claude_account_mcp_logout_skipped", account=account_id, server=server, error=err[:200])
    code, _, err = await _run_cli([binary, "auth", "logout"], env)
    if code != 0:
        log.warning("claude_account_logout_failed", account=account_id, error=err[:200])
    await _forget_account(account_id, row["config_dir"])
    log.info("claude_account_removed", account=account_id)


# ── connectors ───────────────────────────────────────────────────────────

# (key, label, discovery platform or None, server-name hint)
CONNECTORS: tuple[tuple[str, str, str | None, str], ...] = (
    ("slack", "Slack", "slack", "slack"),
    ("gmail", "Gmail", "gmail", "gmail"),
    ("drive", "Google Drive", None, "drive"),
    ("linear", "Linear", "linear", "linear"),
    ("notion", "Notion", "notion", "notion"),
    ("github", "GitHub", None, "github"),
)
# Connectors that can be registered in an account as an MCP server of its own.
MCP_CONNECTORS = ("linear", "notion")

_STATUS_MAP = {"connected": "connected", "needs-auth": "needs_login", "needs_auth": "needs_login",
               "failed": "failed", "pending": "pending", "disabled": "disabled"}


def default_config_file() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def default_mcp_template(connector: str) -> tuple[str, dict[str, str]] | None:
    """(name, {type, url}) of the default login's MCP server for a connector.

    Only URL servers qualify and only their type and URL are taken: headers,
    environment and commands can carry credentials.
    """
    try:
        data = json.loads(default_config_file().read_text())
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return None
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        url = spec.get("url")
        kind = spec.get("type") or ("http" if url else None)
        if not url or kind not in ("http", "sse"):
            continue
        if connector in name.lower() or connector in url.lower():
            return name, {"type": kind, "url": url}
    return None


def connector_matrix(init: dict[str, Any], github: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per connector: state (connected | needs_login | failed | pending | missing),
    kind (claude_ai | mcp | gh), the server name and whether it can be added."""
    from laya.claude_code import discover

    servers = [s for s in init.get("mcp_servers") or [] if isinstance(s, dict)]
    tools = [t for t in init.get("tools") or [] if isinstance(t, str) and t.startswith("mcp__")]
    mapped = discover.map_tools(tools, servers)
    by_norm = {discover.normalize_server_name(s.get("name", "")): s for s in servers}
    out: dict[str, dict[str, Any]] = {}
    for key, label, platform, hint in CONNECTORS:
        if key == "github":
            ok = bool(github and github.get("available"))
            out[key] = {"label": label, "kind": "gh", "state": "connected" if ok else "missing",
                        "server": None, "detail": (github or {}).get("login") or (github or {}).get("problem"),
                        "can_add": False}
            continue
        server = None
        if platform and mapped.get(platform) and mapped[platform].available:
            server = by_norm.get(mapped[platform].server or "")
            state = "connected"
        elif key == "drive" and (served := sorted(
            {p[0] for t in tools if (p := discover.split_tool(t)) and hint in p[0].lower()}
        )):
            server = by_norm.get(served[0])
            state = "connected"
        else:
            server = next((s for s in servers if hint in s.get("name", "").lower()), None)
            state = _STATUS_MAP.get(server.get("status", ""), "failed") if server else "missing"
        name = server.get("name") if server else None
        claude_ai = (name or "").startswith("claude.ai ") or (server or {}).get("source") == "claudeai"
        kind = "claude_ai" if claude_ai or key not in MCP_CONNECTORS else "mcp"
        if server is None and key in MCP_CONNECTORS and default_mcp_template(key) is None:
            kind = "claude_ai"
        out[key] = {
            "label": label, "kind": kind, "state": state, "server": name, "detail": None,
            "can_add": server is None and key in MCP_CONNECTORS and kind == "mcp",
        }
    return out


async def _github_status() -> dict[str, Any]:
    from laya.claude_code import discover

    found = discover.cached()
    gh = found.platforms.get("github") if found else None
    if gh is None:
        gh = await discover.discover_github()
    return {"available": gh.available, "login": gh.identity.get("github_login") if gh.identity else None,
            "problem": gh.problem}


async def check_connectors(account_id: str) -> dict[str, Any]:
    """List the account's connectors (no model call) and store the matrix."""
    from laya.claude_code import cli

    from laya.claude_code import discover

    env = await env_for(account_id)
    try:
        init = await cli.read_init(env=env or None)
        error = None
        discover.store_account_listing(account_id, init)
    except cli.ClaudeCodeError as e:
        init, error = {}, str(e)[:300]
        discover.reset_account_cache(account_id)
    matrix = {
        "connectors": connector_matrix(init, await _github_status()),
        "checked_at": store.now_iso(),
        "error": error,
    }
    await store.set_state(_STATE_PLATFORM, f"connectors:{account_id}", json.dumps(matrix))
    return matrix


async def stored_connectors(account_id: str) -> dict[str, Any] | None:
    state = await store.get_state(_STATE_PLATFORM, f"connectors:{account_id}")
    raw = state.get(f"connectors:{account_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def add_connector(account_id: str, connector: str) -> dict[str, Any]:
    """Register the default login's Linear / Notion server (type and URL only) in an account."""
    if connector not in MCP_CONNECTORS:
        raise AccountError("Only Linear and Notion can be added here")
    if account_id == DEFAULT_ID:
        raise AccountError("Add servers to your default login in Claude Code itself")
    env = await env_for(account_id)
    template = default_mcp_template(connector)
    if template is None:
        raise AccountError(f"Your default Claude Code login has no {connector.title()} server to copy")
    name, spec = template
    row = await get_row(account_id)
    if name in user_mcp_servers(row["config_dir"]):
        return await check_connectors(account_id)
    code, out, err = await _run_cli(
        [get_agent_binary("claude_code"), "mcp", "add", "--scope", "user",
         "--transport", spec["type"], name, spec["url"]],
        env,
    )
    if code != 0:
        raise AccountError(f"claude mcp add failed: {(err or out).strip()[:300]}", status=502)
    log.info("claude_account_connector_added", account=account_id, server=name)
    return await check_connectors(account_id)


async def start_connector_login(account_id: str, connector: str) -> dict[str, Any]:
    """Run ``claude mcp login <server> --no-browser`` for an account's Linear / Notion server."""
    _ensure_idle()
    if connector not in MCP_CONNECTORS:
        raise AccountError("Only Linear and Notion sign in here")
    if account_id == DEFAULT_ID:
        raise AccountError("Sign in to your default login's servers in Claude Code itself")
    row = await get_row(account_id)
    if row is None:
        raise AccountError("Account not found", status=404)
    server = next((n for n in user_mcp_servers(row["config_dir"]) if connector in n.lower()), None)
    if server is None:
        raise AccountError(f"Add the {connector.title()} server to this account first")
    flow = LoginFlow(id=uuid.uuid4().hex, kind="connector", account_id=account_id,
                     new_account=False, server=server, platform=connector)
    return await _start_flow(
        flow, [get_agent_binary("claude_code"), "mcp", "login", server, "--no-browser"],
        account_env(row), row["config_dir"],
    )


# ── overview ─────────────────────────────────────────────────────────────


async def _usage(account_id: str) -> dict[str, Any]:
    """Usage windows, when they were last seen and the poll's per-model lines."""
    row = await rate_row(account_id)
    rate_at = await _rate_updated_epoch(row) if row else None
    polled = await stored_usage(account_id)
    poll_at = float(polled["at"]) if polled and polled.get("at") else None
    newest = max([t for t in (rate_at, poll_at) if t is not None], default=None)
    from datetime import datetime, timezone

    return {
        "windows": await windows_of(account_id),
        "as_of": datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else None,
        "models": (polled or {}).get("models") or [],
    }


async def overview(refresh: bool = False) -> dict[str, Any]:
    """Everything the Settings tab shows."""
    if refresh:
        await poll_all_usage()
    default = await default_status(force=refresh)
    rows = await list_rows()
    own = has_own_accounts(rows)
    accounts = [{
        "id": DEFAULT_ID, "default": True, "email": default["email"], "plan": default["plan"],
        "logged_in": default["logged_in"],
        "in_use": not own,
        "problem": None if default["logged_in"] else
        "Claude Code is not logged in. Run `claude auth login` in a terminal.",
        "usage": await _usage(DEFAULT_ID),
        "connectors": await stored_connectors(DEFAULT_ID),
    }]
    eligible = own_eligible(rows)
    for row in rows:
        if not row.get("status_checked_at") and not login_running_for(row["id"]):
            with contextlib.suppress(AccountError):
                await refresh_status(row["id"])
                row = await get_row(row["id"]) or row
        pending = not row.get("email") and not row["logged_in"]
        problem = None
        if pending:
            pass
        elif not row["logged_in"]:
            problem = "Signed out. Sign in again to use this account."
        elif row["id"] not in eligible:
            problem = "The same Claude account as another entry; Laya does not use it."
        accounts.append({
            "id": row["id"], "default": False, "email": row.get("email"), "plan": row.get("plan"),
            "logged_in": bool(row["logged_in"]), "in_use": row["id"] in eligible,
            "pending": pending,
            "problem": problem,
            "usage": await _usage(row["id"]),
            "connectors": await stored_connectors(row["id"]),
        })
    return {
        "accounts": accounts,
        "current": await current_account(),
        "thresholds": _thresholds(),
        "login": login_state(),
        "connectors_url": CONNECTORS_URL,
    }


async def shutdown() -> None:
    if _flow is not None and _flow.active:
        with contextlib.suppress(Exception):
            await _end(_flow, "cancelled", None)
