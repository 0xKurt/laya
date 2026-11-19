# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Claude accounts: several Claude subscriptions as sources of usage quota.

The **default account** is the user's normal Claude Code login (no
``CLAUDE_CONFIG_DIR`` override). It is implicit: it has no row and needs no
setup. Every **added account** is a separate Claude Code configuration
directory under ``~/.laya/claude-accounts/<id>/`` (mode 0700). Laya runs
``claude`` with ``CLAUDE_CONFIG_DIR=<dir>`` for it, so its login, its MCP
servers and their OAuth tokens stay in the CLI's own storage; this module
never reads or stores a token.

Parts:

- registry: the module-owned table ``claude_code_accounts`` (created with
  ``CREATE TABLE IF NOT EXISTS``), the email / plan from
  ``claude auth status --json`` and whether the account is eligible
  (logged in, not the same Claude account as another one);
- routing: :func:`pick` chooses the account for an inference stage or a chat
  turn (``llm/agent_backend.py``), sticky with hysteresis and a minimum
  dwell, and :func:`failover` the next account after a rate-limit
  rejection. Collectors, sending, My work, discovery and the terminal do not
  route; they run on the default account;
- usage: :func:`record` stores each turn's ``rate_limit_info`` in
  ``agent_rate_limit_state`` under ``claude_code`` (default) or
  ``claude_code@<id>``; :func:`native_block` tells the agent budget whether
  every account is over its threshold;
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
    created_at        TEXT NOT NULL
)"""

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
        await db.commit()
        db._laya_claude_accounts_table = True
    return db


def agent_row(account_id: str) -> str:
    """The ``agent_rate_limit_state`` row of an account."""
    return AGENT_ID if account_id == DEFAULT_ID else f"{AGENT_ID}@{account_id}"


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


async def _run_cli(args: list[str], env: dict[str, str] | None = None, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run a short ``claude`` subcommand; returns (exit code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_cli_env(env),
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
    await _update_row(
        account_id,
        logged_in=1 if status["logged_in"] else 0,
        email=status["email"] if status["logged_in"] else row.get("email"),
        plan=status["plan"] if status["logged_in"] else row.get("plan"),
        org_name=status["org_name"] if status["logged_in"] else row.get("org_name"),
        status_checked_at=store.now_iso(),
    )
    return status


def _same_account(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


async def eligible_ids() -> list[str]:
    """Accounts routing may use, in preference order: the default first, then
    the added accounts that are logged in and are not a Claude account that
    is already listed."""
    ids = [DEFAULT_ID]
    try:
        rows = await list_rows()
    except Exception:
        return ids
    if not rows:
        return ids
    seen = [(_default_status[1] if _default_status else {}).get("email")]
    for row in rows:
        if not row["logged_in"] or not Path(row["config_dir"]).is_dir():
            continue
        if any(_same_account(row.get("email"), e) for e in seen):
            continue
        seen.append(row.get("email"))
        ids.append(row["id"])
    return ids


# ── usage ────────────────────────────────────────────────────────────────


def _thresholds() -> dict[str, float]:
    from laya.pipeline.agent_budget import DEFAULT_PAUSE_AT_UTILIZATION, get_agent_budget_config

    try:
        return dict(get_agent_budget_config().get("pause_at_utilization") or DEFAULT_PAUSE_AT_UTILIZATION)
    except Exception:
        return dict(DEFAULT_PAUSE_AT_UTILIZATION)


async def windows_of(account_id: str, now: float | None = None) -> dict[str, dict[str, Any]]:
    """The account's current usage windows (see agent_budget.rate_limit_windows)."""
    from laya.pipeline.agent_budget import get_rate_limit_raw, rate_limit_windows

    raw = await get_rate_limit_raw(agent_row(account_id))
    return rate_limit_windows(raw or {}, now=now)


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


async def _decide(commit: bool) -> str:
    order = await eligible_ids()
    if len(order) == 1:
        if commit and _route.current != DEFAULT_ID:
            _route.current, _route.since = DEFAULT_ID, time.time()
        return DEFAULT_ID
    now = time.time()
    windows = {a: await windows_of(a, now) for a in order}
    chosen = choose(order, windows, _thresholds(), _route.current, _route.since, now)
    if commit and chosen != _route.current:
        log.info("claude_account_switch", previous=_route.current, account=chosen)
        _route.current, _route.since = chosen, now
    return chosen


async def pick(purpose: str = "inference") -> tuple[str, dict[str, str]]:
    """The account for one inference stage or chat turn and its environment.

    Never raises: any failure routes to the default account.
    """
    try:
        account_id = await _decide(commit=True)
        if account_id == DEFAULT_ID:
            return DEFAULT_ID, {}
        return account_id, account_env(await get_row(account_id))
    except Exception as e:
        log.warning("claude_account_pick_failed", purpose=purpose, error=str(e))
        return DEFAULT_ID, {}


async def current_account() -> str:
    """The account processing uses now (what the next :func:`pick` returns)."""
    try:
        return await _decide(commit=False)
    except Exception:
        return DEFAULT_ID


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
        log.info("claude_account_failover", failed=failed, account=nxt)
        _route.current, _route.since = nxt, now
        return nxt, (await env_for(nxt))
    except Exception as e:
        log.warning("claude_account_failover_failed", error=str(e))
        return None


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
        await agent_budget.record_rate_limit(agent_row(account_id or DEFAULT_ID), info)
    except Exception as e:
        log.warning("claude_account_record_failed", account=account_id, error=str(e))


async def native_block(thresholds: dict[str, float]) -> tuple[bool, str | None, str]:
    """(every eligible account over its threshold, earliest recovery as a db
    timestamp or None, reason) for the agent budget."""
    from laya.db.timeutil import db_ts_from_epoch

    order = await eligible_ids()
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OSC8_URL_RE = re.compile(r"\x1b\]8;[^;]*;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")
_URL_RE = re.compile(r"https://[^\s\x07\x1b'\"<>]+")


def parse_login_url(text: str) -> str | None:
    """The authorize URL a ``claude auth login`` / ``claude mcp login`` printed."""
    m = _OSC8_URL_RE.search(text)
    if m:
        return m.group(1)
    m = _URL_RE.search(_ANSI_RE.sub("", text))
    return m.group(0) if m else None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in _ANSI_RE.sub("", text).replace("\r", "\n").split("\n")]
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "account_id": self.account_id,
            "server": self.server, "platform": self.platform, "state": self.state,
            "url": self.url, "error": self.error, "started_at": self.started_at,
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
            raise AccountError("The default account is your normal Claude Code login.")
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
    default_email = (await default_status(force=True)).get("email")
    if any(_same_account(status["email"], e) for e in [default_email, *(r.get("email") for r in others)]):
        # Usually the browser was still signed in to the account Laya already uses.
        await _run_cli([get_agent_binary("claude_code"), "auth", "logout"], account_env(row))
        await _end(
            flow, "failed",
            f"{status['email']} is already one of your Claude accounts. Sign in to the other "
            "account on claude.ai in your browser (or use a private window) and add it again.",
        )
        return
    await _update_row(
        flow.account_id, logged_in=1, email=status["email"], plan=status["plan"],
        org_name=status["org_name"], status_checked_at=store.now_iso(),
    )
    await _end(flow, "done", None)
    await _after_account_added()
    with contextlib.suppress(Exception):
        await check_connectors(flow.account_id)


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
    await store.delete_state(_STATE_PLATFORM, [f"connectors:{account_id}"])
    _remove_account_dir(config_dir)
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
        raise AccountError("The default account is your normal Claude Code login and cannot be removed.")
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

    env = await env_for(account_id)
    try:
        init = await cli.read_init(env=env or None)
        error = None
    except cli.ClaudeCodeError as e:
        init, error = {}, str(e)[:300]
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
    from laya.pipeline.agent_budget import get_rate_limit_raw, rate_limit_windows

    db = await _db()
    rows = await db.execute_fetchall(
        "SELECT updated_at FROM agent_rate_limit_state WHERE agent_id = ?", (agent_row(account_id),)
    )
    raw = await get_rate_limit_raw(agent_row(account_id))
    return {
        "windows": rate_limit_windows(raw or {}),
        "as_of": rows[0]["updated_at"] if rows else None,
    }


async def overview(refresh: bool = False) -> dict[str, Any]:
    """Everything the Settings tab shows."""
    default = await default_status(force=refresh)
    accounts = [{
        "id": DEFAULT_ID, "default": True, "email": default["email"], "plan": default["plan"],
        "logged_in": default["logged_in"], "problem": None if default["logged_in"] else
        "Claude Code is not logged in. Run `claude auth login` in a terminal.",
        "usage": await _usage(DEFAULT_ID),
        "connectors": await stored_connectors(DEFAULT_ID),
    }]
    eligible = await eligible_ids()
    for row in await list_rows():
        if refresh or not row.get("status_checked_at"):
            with contextlib.suppress(AccountError):
                await refresh_status(row["id"])
                row = await get_row(row["id"]) or row
        problem = None
        if not row["logged_in"]:
            problem = "Signed out. Sign in again to use this account."
        elif row["id"] not in eligible:
            problem = "The same Claude account as another entry; Laya does not use it."
        accounts.append({
            "id": row["id"], "default": False, "email": row.get("email"), "plan": row.get("plan"),
            "logged_in": bool(row["logged_in"]), "problem": problem,
            "usage": await _usage(row["id"]),
            "connectors": await stored_connectors(row["id"]),
        })
    flow = current_flow()
    return {
        "accounts": accounts,
        "current": await current_account(),
        "thresholds": _thresholds(),
        "login": flow.to_dict() if flow is not None else None,
        "connectors_url": CONNECTORS_URL,
    }


async def shutdown() -> None:
    if _flow is not None and _flow.active:
        with contextlib.suppress(Exception):
            await _end(_flow, "cancelled", None)
