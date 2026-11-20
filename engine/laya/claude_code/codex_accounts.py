# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Codex logins: the Codex CLI's ChatGPT subscriptions as sources of usage quota.

The counterpart of ``accounts.py`` (Claude Code) for ``codex_cli``. Both keep
their logins in the module-owned table ``claude_code_accounts``, told apart by
its ``agent_id`` column.

- The **CLI login** (``DEFAULT_ID``) is whatever ``codex`` is logged in to (no
  ``CODEX_HOME`` override). Like Claude's, it serves Laya only while no Codex
  login has been added.
- Every **added login** is a Codex home ``~/.laya/codex-homes/<id>/`` (0700);
  Laya runs ``codex`` with ``CODEX_HOME=<dir>`` for it, so its credentials stay
  in Codex's own storage. Sign-in is ``codex login --device-auth`` in a PTY:
  the UI shows the page and the one-time code, the user finishes in the browser.
- **Identity and usage** come from ``codex app-server`` (JSON-RPC over stdio,
  no model call): ``account/read`` (email, plan, ChatGPT account id) and
  ``account/rateLimits/read`` (used percent and reset per window), stored in
  ``agent_rate_limit_state`` under ``codex_cli@<login>`` in the shape Claude
  Code's ``rate_limit_info`` has, so the agent budget and the routing read both
  agents alike.
- A turn that hits the limit fails with an error naming the reset time
  ("… or try again at 10:14 AM."); :func:`limit_info` turns it into a
  rejection with ``resetsAt``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from laya.config import LAYA_HOME, get_agent_binary

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


def agent_row(login_id: str, account_id: str | None = None) -> str:
    """The ``agent_rate_limit_state`` row of a Codex login.

    An added login has its own row. The CLI login is keyed by the ChatGPT
    account it is logged in to (so a ``codex login`` to another account starts
    from a clean row), or by ``cli`` while that account is unknown.
    """
    if login_id != DEFAULT_ID:
        return f"{AGENT_ID}@{login_id}"
    return f"{AGENT_ID}@acct:{account_id}" if account_id else f"{AGENT_ID}@cli"


async def rate_row(login_id: str) -> str:
    login_id = login_id or DEFAULT_ID
    if login_id != DEFAULT_ID:
        return agent_row(login_id)
    return agent_row(DEFAULT_ID, (await default_status()).get("account_id"))


async def record(login_id: str | None, info: dict | None) -> None:
    """Store a Codex turn's limit signal under the login's row. Never raises."""
    if not info:
        return
    from laya.pipeline import agent_budget

    try:
        await agent_budget.record_rate_limit(await rate_row(login_id or DEFAULT_ID), info)
    except Exception as e:
        log.warning("codex_login_record_failed", login=login_id, error=str(e))


# ── app-server (identity, usage, MCP inventory; no model call) ───────────


HOMES_DIR = LAYA_HOME / "codex-homes"
APP_SERVER_TIMEOUT = 60.0
STATUS_CACHE_SECONDS = 60.0
USAGE_POLL_SECONDS = 600.0


class AppServerError(RuntimeError):
    """``codex app-server`` did not answer."""


def cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    from laya.claude_code.accounts import _cli_env

    return _cli_env(extra)


async def app_server(
    requests: list[tuple[str, dict[str, Any] | None]],
    env: dict[str, str] | None = None,
    timeout: float = APP_SERVER_TIMEOUT,
    args: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Answer ``requests`` (method, params) with one ``codex app-server`` process.

    Returns each request's response object (``result`` or ``error``) in order.
    ``args`` are extra arguments (``-c key=value`` overrides).
    """
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="laya-codex-app-")
    try:
        proc = await asyncio.create_subprocess_exec(
            get_agent_binary(AGENT_ID), "app-server", *(args or []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=cli_env(env), cwd=tmpdir,
            limit=16 * 1024 * 1024,
        )
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise AppServerError(f"codex app-server did not start: {e}") from e

    async def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    async def answer(req_id: int) -> dict[str, Any]:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise AppServerError("codex app-server ended before answering")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg

    async def run() -> list[dict[str, Any]]:
        await send({"id": 0, "method": "initialize",
                    "params": {"clientInfo": {"name": "laya", "title": "Laya", "version": "1"}}})
        await answer(0)
        await send({"method": "initialized"})
        out = []
        for i, (method, params) in enumerate(requests, start=1):
            req: dict[str, Any] = {"id": i, "method": method}
            if params is not None:
                req["params"] = params
            await send(req)
            out.append(await answer(i))
        return out

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise AppServerError(f"codex app-server did not answer within {int(timeout)}s") from e
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)


def parse_account(resp: dict[str, Any]) -> dict[str, Any]:
    """``account/read`` → {logged_in, email, plan, account_id}."""
    result = resp.get("result") if isinstance(resp, dict) else None
    account = (result or {}).get("account") if isinstance(result, dict) else None
    routing = (result or {}).get("workspaceRouting") if isinstance(result, dict) else None
    if not isinstance(account, dict):
        return {"logged_in": False, "email": None, "plan": None, "account_id": None, "auth": None}
    return {
        "logged_in": True,
        "email": account.get("email") or None,
        "plan": account.get("planType") or None,
        "account_id": (routing or {}).get("chatgptAccountId") if isinstance(routing, dict) else None,
        "auth": account.get("type") or None,
    }


_WINDOW_BY_MINUTES = {300: "five_hour", 10080: "seven_day"}


def rate_limit_info(resp: dict[str, Any]) -> dict[str, Any] | None:
    """``account/rateLimits/read`` → a Claude-shaped ``rate_limit_info``.

    Each Codex window (``primary`` / ``secondary``) becomes a ``unifiedWindows``
    entry named by its length (5 h → ``five_hour``, 7 d → ``seven_day``) with
    utilization = used percent / 100; a window at 100 % or a reached limit is
    ``rejected``.
    """
    result = resp.get("result") if isinstance(resp, dict) else None
    limits = (result or {}).get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return None
    reached = bool(limits.get("rateLimitReachedType")) or result.get("ordinaryUsageAllowed") is False
    windows: dict[str, dict[str, Any]] = {}
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict) or w.get("usedPercent") is None:
            continue
        minutes = int(w.get("windowDurationMins") or 0)
        name = _WINDOW_BY_MINUTES.get(minutes) or (f"{minutes // 60}h" if minutes else slot)
        used = float(w["usedPercent"]) / 100.0
        windows[name] = {
            "utilization": used,
            "resetsAt": w.get("resetsAt"),
            "status": "rejected" if used >= 1.0 else "allowed",
        }
    if not windows:
        return None
    if reached and not any(w["status"] == "rejected" for w in windows.values()):
        # The server says no, whichever window it is: the fullest one is it.
        fullest = max(windows.values(), key=lambda w: w["utilization"] or 0)
        fullest["status"] = "rejected"
    rejected = [n for n, w in windows.items() if w["status"] == "rejected"]
    top = rejected[0] if rejected else max(windows, key=lambda n: windows[n]["utilization"] or 0)
    return {
        "status": "rejected" if rejected else "allowed",
        "resetsAt": windows[top]["resetsAt"],
        "rateLimitType": top,
        "utilization": windows[top]["utilization"],
        "unifiedWindows": windows,
        "source": "codex_app_server",
    }


async def account_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Identity of a Codex home (``env`` = its ``CODEX_HOME``; none = the CLI login)."""
    try:
        (resp,) = await app_server([("account/read", {"refreshToken": False})], env)
    except AppServerError as e:
        log.info("codex_account_status_failed", error=str(e))
        return {"logged_in": False, "email": None, "plan": None, "account_id": None,
                "auth": None, "error": str(e)}
    return parse_account(resp)


_default_status: tuple[float, dict[str, Any]] | None = None


async def default_status(force: bool = False) -> dict[str, Any]:
    """Identity of the CLI login, cached for STATUS_CACHE_SECONDS. Never raises."""
    global _default_status
    if not force and _default_status and time.monotonic() - _default_status[0] < STATUS_CACHE_SECONDS:
        return _default_status[1]
    from laya.config import detect_agent_paths

    if not detect_agent_paths().get(AGENT_ID):
        status = {"logged_in": False, "email": None, "plan": None, "account_id": None,
                  "auth": None, "error": "Codex CLI not found"}
    else:
        status = await account_status()
    _default_status = (time.monotonic(), status)
    return status


# ── registry (rows of claude_code_accounts with agent_id = codex_cli) ────


async def _db():
    from laya.claude_code.accounts import _db as accounts_db

    return await accounts_db()


async def list_rows() -> list[dict[str, Any]]:
    db = await _db()
    rows = await db.execute_fetchall(
        "SELECT * FROM claude_code_accounts WHERE agent_id = ? ORDER BY created_at, id", (AGENT_ID,)
    )
    return [dict(r) for r in rows]


async def get_row(login_id: str) -> dict[str, Any] | None:
    db = await _db()
    rows = await db.execute_fetchall(
        "SELECT * FROM claude_code_accounts WHERE id = ? AND agent_id = ?", (login_id, AGENT_ID)
    )
    return dict(rows[0]) if rows else None


async def _update_row(login_id: str, **fields: Any) -> None:
    db = await _db()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE claude_code_accounts SET {assignments} WHERE id = ?", (*fields.values(), login_id)
    )
    await db.commit()


def login_env(row: dict[str, Any] | None) -> dict[str, str]:
    """Environment overrides that make ``codex`` use a login (none for the CLI login)."""
    return {"CODEX_HOME": row["config_dir"]} if row else {}


async def env_for(login_id: str) -> dict[str, str]:
    if login_id == DEFAULT_ID:
        return {}
    from laya.claude_code.accounts import AccountError

    row = await get_row(login_id)
    if row is None:
        raise AccountError("Login not found", status=404)
    return login_env(row)


def _identity(row: dict[str, Any]) -> str | None:
    if row.get("org_id"):
        return f"acct:{row['org_id']}"
    email = (row.get("email") or "").strip().lower()
    return f"email:{email}" if email else None


def own_eligible(rows: list[dict[str, Any]]) -> list[str]:
    """Added logins routing may use, in creation order: logged in, home present,
    not the same ChatGPT account as an earlier row."""
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
    """Codex logins routing may use, in preference order: the added ones that
    are eligible; the CLI login only while none is added and it is logged in."""
    from laya.claude_code.accounts import has_own_accounts

    rows = await list_rows()
    if has_own_accounts(rows):
        return own_eligible(rows)
    return [DEFAULT_ID] if (await default_status()).get("logged_in") else []


async def label(login_id: str) -> str:
    if login_id == DEFAULT_ID:
        email = (await default_status()).get("email")
        return f"the Codex CLI login{f' ({email})' if email else ''}"
    row = await get_row(login_id)
    return (row or {}).get("email") or f"Codex login {login_id}"


# ── usage and routing ────────────────────────────────────────────────────


async def windows_of(login_id: str, now: float | None = None) -> dict[str, dict[str, Any]]:
    from laya.pipeline.agent_budget import get_rate_limit_raw, rate_limit_windows

    raw = await get_rate_limit_raw(await rate_row(login_id))
    return rate_limit_windows(raw or {}, now=now)


_last_poll: dict[str, float] = {}


async def poll_usage(login_id: str) -> dict[str, Any]:
    """Read a login's identity and rate limits from ``codex app-server`` and store them."""
    _last_poll[login_id] = time.monotonic()
    from laya.claude_code import accounts

    if accounts.login_running_for(login_id):
        return {"polled": False, "logged_in": None, "error": "sign-in running"}
    if login_id == DEFAULT_ID:
        status = await default_status(force=True)
        env: dict[str, str] = {}
    else:
        row = await get_row(login_id)
        if row is None:
            raise accounts.AccountError("Login not found", status=404)
        env = login_env(row)
        status = await account_status(env)
        await _store_status(row, status)
    if not status.get("logged_in"):
        return {"polled": False, "logged_in": False, "error": status.get("error")}
    try:
        (resp,) = await app_server([("account/rateLimits/read", None)], env)
    except AppServerError as e:
        return {"polled": False, "logged_in": True, "error": str(e)}
    info = rate_limit_info(resp)
    if info is None:
        return {"polled": False, "logged_in": True, "error": "no rate limits reported"}
    from laya.pipeline import agent_budget

    row_name = agent_row(login_id, status.get("account_id"))
    await agent_budget.record_rate_limit(row_name, info)
    return {"polled": True, "logged_in": True, "error": None}


async def _store_status(row: dict[str, Any], status: dict[str, Any]) -> None:
    from laya.claude_code import store

    logged_in = bool(status.get("logged_in"))
    if status.get("error") and not logged_in and row["logged_in"]:
        # The app-server failed to answer; that says nothing about the login.
        return
    new_acct = status.get("account_id") if logged_in else None
    if new_acct and row.get("org_id") and new_acct != row["org_id"]:
        db = await _db()
        await db.execute("DELETE FROM agent_rate_limit_state WHERE agent_id = ?", (agent_row(row["id"]),))
        await db.commit()
    await _update_row(
        row["id"],
        logged_in=1 if logged_in else 0,
        email=status.get("email") if logged_in else row.get("email"),
        plan=status.get("plan") if logged_in else row.get("plan"),
        org_id=new_acct or row.get("org_id"),
        status_checked_at=store.now_iso(),
    )


def codex_used() -> bool:
    """Whether Laya's settings route anything to Codex (or to Automatic)."""
    from laya.config import load_settings

    models = (load_settings().get("models") or {}).values()
    return any(str(m).startswith(("agent/codex_cli", "agent/auto")) for m in models)


async def poll_due_usage() -> list[str]:
    """Poll every login whose last poll is ``USAGE_POLL_SECONDS`` old (service loop)."""
    rows = await list_rows()
    if not rows and not codex_used():
        return []
    targets = [DEFAULT_ID] if not rows else []
    targets += [r["id"] for r in rows if r["logged_in"] or r.get("email")]
    polled: list[str] = []
    now = time.monotonic()
    for login_id in targets:
        last = _last_poll.get(login_id)
        if last is not None and now - last < USAGE_POLL_SECONDS:
            continue
        try:
            if (await poll_usage(login_id)).get("polled"):
                polled.append(login_id)
        except Exception as e:
            log.warning("codex_login_usage_poll_failed", login=login_id, error=str(e))
    return polled


async def poll_all_usage() -> None:
    rows = await list_rows()
    targets = [DEFAULT_ID] + [r["id"] for r in rows if r["logged_in"] or r.get("email")]

    async def one(login_id: str) -> None:
        try:
            await poll_usage(login_id)
        except Exception as e:
            log.warning("codex_login_usage_poll_failed", login=login_id, error=str(e))

    await asyncio.gather(*(one(t) for t in targets))


from laya.claude_code.accounts import _Route  # noqa: E402

_route = _Route()


def _thresholds() -> dict[str, float]:
    from laya.claude_code.accounts import _thresholds as thresholds

    return thresholds()


async def pick(purpose: str = "inference") -> tuple[str, dict[str, str]]:
    """The Codex login for one turn and its environment (sticky, like Claude's)."""
    from laya.claude_code.accounts import NoEligibleAccount, choose

    order = await eligible_ids()
    if not order:
        raise NoEligibleAccount("No Codex login Laya can use. Add one in Settings → Models → Access.")
    if len(order) == 1:
        chosen = order[0]
    else:
        now = time.time()
        windows = {a: await windows_of(a, now) for a in order}
        chosen = choose(order, windows, _thresholds(), _route.current, _route.since, now)
    if chosen != _route.current:
        _route.current, _route.since = chosen, time.time()
    return chosen, await env_for(chosen)


async def current_login() -> str | None:
    from laya.claude_code.accounts import choose

    try:
        order = await eligible_ids()
        if not order:
            return None
        if len(order) == 1:
            return order[0]
        now = time.time()
        windows = {a: await windows_of(a, now) for a in order}
        return choose(order, windows, _thresholds(), _route.current, _route.since, now)
    except Exception:
        return None


async def failover(failed: str) -> tuple[str, dict[str, str]] | None:
    """The next Codex login with room after ``failed`` was rejected, or None."""
    from laya.claude_code.accounts import account_over, headroom

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
    log.info("codex_login_failover", failed=failed, login=nxt)
    _route.current, _route.since = nxt, time.time()
    return nxt, await env_for(nxt)


async def native_block(thresholds: dict[str, float]) -> tuple[bool, str | None, str]:
    """(every eligible Codex login over its threshold, earliest recovery, reason)."""
    from laya.claude_code.accounts import window_over
    from laya.db.timeutil import db_ts_from_epoch

    order = await eligible_ids()
    if not order:
        return True, None, f"{PAUSE_REASON} (no Codex login can be used)"
    now = time.time()
    recoveries: list[int] = []
    for a in order:
        windows = await windows_of(a, now)
        over = [n for n, w in windows.items() if window_over(n, w, thresholds)]
        if not over:
            return False, None, ""
        resets = [int(windows[n]["resets_at"]) for n in over if windows[n].get("resets_at")]
        if resets:
            recoveries.append(max(resets))
    until = db_ts_from_epoch(min(recoveries)) if recoveries else None
    return True, until, PAUSE_REASON


PAUSE_REASON = "codex_cli: every Codex login is over its usage threshold"


# ── sign-in (`codex login --device-auth` in a PTY) ───────────────────────

# The one-time code the CLI prints under "Enter this one-time code", e.g. "SLAW-2JV1T".
_DEVICE_CODE_RE = re.compile(r"one-time code[^\n]*\n\s*([A-Z0-9]{4,}-[A-Z0-9]{4,})")


def parse_device_code(text: str) -> str | None:
    from laya.claude_code.accounts import strip_escapes

    m = _DEVICE_CODE_RE.search(strip_escapes(text).replace("\r", ""))
    return m.group(1) if m else None


def _new_home(login_id: str) -> Path:
    HOMES_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(HOMES_DIR, 0o700)
    path = HOMES_DIR / login_id
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _remove_home(path: str) -> None:
    """Delete a Codex home, only when it lies inside HOMES_DIR."""
    p = Path(path).resolve()
    if p.parent != HOMES_DIR.resolve():
        log.warning("codex_home_outside_base", path=str(p))
        return
    shutil.rmtree(p, ignore_errors=True)


async def start_login(login_id: str | None = None) -> dict[str, Any]:
    """Sign in a new Codex login (``login_id`` None) or an added one again."""
    from laya.claude_code import accounts, store

    accounts._ensure_idle()
    if login_id is None:
        login_id = uuid.uuid4().hex[:10]
        path = _new_home(login_id)
        db = await _db()
        await db.execute(
            "INSERT INTO claude_code_accounts (id, config_dir, created_at, agent_id) VALUES (?, ?, ?, ?)",
            (login_id, str(path), store.now_iso(), AGENT_ID),
        )
        await db.commit()
        new = True
    else:
        if login_id == DEFAULT_ID:
            raise accounts.AccountError("The Codex CLI login is managed in Codex itself.")
        row = await get_row(login_id)
        if row is None:
            raise accounts.AccountError("Login not found", status=404)
        path = Path(row["config_dir"])
        new = False
    flow = accounts.LoginFlow(
        id=uuid.uuid4().hex, kind="account", account_id=login_id, new_account=new, agent_id=AGENT_ID,
    )
    try:
        return await accounts._start_flow(
            flow, [get_agent_binary(AGENT_ID), "login", "--device-auth"],
            {"CODEX_HOME": str(path)}, str(path),
        )
    except accounts.AccountError:
        if new:
            await forget(login_id, str(path))
        raise


async def finish_login(flow: Any, code: int, text: str) -> None:
    """Called when ``codex login`` exits: verify, refuse duplicates, store."""
    from laya.claude_code import accounts

    row = await get_row(flow.account_id)
    status = await account_status(login_env(row)) if row else {"logged_in": False}
    if code != 0 or not status.get("logged_in"):
        await accounts._end(flow, "failed", accounts._last_line(text) or "The sign-in failed.")
        return
    others = [r for r in await list_rows() if r["id"] != flow.account_id and r["logged_in"]]
    mine = _identity({"org_id": status.get("account_id"), "email": status.get("email")})
    if mine and any(_identity(r) == mine for r in others):
        await accounts._run_cli([get_agent_binary(AGENT_ID), "logout"], login_env(row))
        await accounts._end(
            flow, "failed",
            f"{status.get('email') or 'This account'} is already one of your Codex logins. Sign in to "
            "the other account on chatgpt.com in your browser (or use a private window) and add it again.",
        )
        return
    await _store_status(row, status)
    await accounts._end(flow, "done", None)
    with contextlib.suppress(Exception):
        await poll_usage(flow.account_id)


async def forget(login_id: str, home: str) -> None:
    db = await _db()
    await db.execute("DELETE FROM claude_code_accounts WHERE id = ? AND agent_id = ?", (login_id, AGENT_ID))
    await db.execute("DELETE FROM agent_rate_limit_state WHERE agent_id = ?", (agent_row(login_id),))
    await db.commit()
    from laya.claude_code import store

    await store.delete_state("accounts", [f"connectors:{login_id}"])
    _remove_home(home)
    _last_poll.pop(login_id, None)
    if _route.current == login_id:
        _route.current = None


async def remove(login_id: str) -> None:
    """Log the Codex home out, then delete it."""
    from laya.claude_code import accounts

    if login_id == DEFAULT_ID:
        raise accounts.AccountError("The Codex CLI login is managed in Codex itself and cannot be removed here.")
    row = await get_row(login_id)
    if row is None:
        raise accounts.AccountError("Login not found", status=404)
    flow = accounts.current_flow()
    if flow is not None and flow.active and flow.account_id == login_id:
        await accounts._end(flow, "cancelled", None)
    code, _, err = await accounts._run_cli([get_agent_binary(AGENT_ID), "logout"], login_env(row))
    if code != 0:
        log.info("codex_logout_failed", login=login_id, error=err[:200])
    await forget(login_id, row["config_dir"])
    log.info("codex_login_removed", login=login_id)


# ── overview ─────────────────────────────────────────────────────────────


async def _usage(login_id: str) -> dict[str, Any]:
    from laya.claude_code.accounts import _rate_updated_epoch

    row = await rate_row(login_id)
    at = await _rate_updated_epoch(row)
    from datetime import timezone

    return {
        "windows": await windows_of(login_id),
        "as_of": datetime.fromtimestamp(at, timezone.utc).isoformat() if at else None,
        "models": [],
    }


async def overview_entries() -> list[dict[str, Any]]:
    """The Codex logins as entries of the Access list (same shape as Claude accounts)."""
    from laya.claude_code import accounts

    rows = await list_rows()
    own = accounts.has_own_accounts(rows)
    default = await default_status()
    current = await current_login()
    out: list[dict[str, Any]] = [{
        "id": DEFAULT_ID, "agent_id": AGENT_ID, "default": True,
        "email": default.get("email"), "plan": default.get("plan"),
        "logged_in": bool(default.get("logged_in")),
        "in_use": not own and bool(default.get("logged_in")),
        "current": current == DEFAULT_ID,
        "problem": None if default.get("logged_in") else
        (default.get("error") if default.get("error") == "Codex CLI not found"
         else "Codex is not logged in. Run `codex login` in a terminal, or add a login here."),
        "usage": await _usage(DEFAULT_ID) if default.get("logged_in") else {"windows": {}, "as_of": None, "models": []},
        "connectors": await accounts.stored_connectors(DEFAULT_ID),
    }]
    eligible = own_eligible(rows)
    for row in rows:
        pending = not row.get("email") and not row["logged_in"]
        problem = None
        if not pending and not row["logged_in"]:
            problem = "Signed out. Sign in again to use this login."
        elif not pending and row["id"] not in eligible:
            problem = "The same account as another entry; Laya does not use it."
        out.append({
            "id": row["id"], "agent_id": AGENT_ID, "default": False,
            "email": row.get("email"), "plan": row.get("plan"),
            "logged_in": bool(row["logged_in"]), "in_use": row["id"] in eligible,
            "current": current == row["id"],
            "pending": pending, "problem": problem,
            "usage": await _usage(row["id"]),
            "connectors": await accounts.stored_connectors(row["id"]),
        })
    return out
