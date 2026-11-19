# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Plain terminal sessions that run the user's own ``claude`` CLI in a PTY.

A terminal session is the ``claude`` binary (``config.get_agent_binary("claude_code")``)
started in a pseudo-terminal with the user's normal environment and Claude Code
configuration. Laya adds exactly one thing to the command line: ``--mcp-config``
pointing at Laya's own Streamable HTTP MCP endpoint, with a per-session bearer
token limited to the read tools (``laya.mcp.turn_tokens``, no expiry, revoked
when the session closes). Nothing else is set: no permission mode, no setting
sources, no agents, no system prompt.

Each session is started with ``--session-id <uuid>`` so the Claude conversation
can be resumed later with ``--resume <uuid>``. A small module-owned table
(``claude_code_terminal_sessions`` in ``store.py``) remembers sessions so closed
ones can be listed and resumed; the processes
themselves live only in this module and end with the engine.

The UI talks to a session over the websocket ``/claude-code/terminal/{id}/pty``.
Output is kept in a ring buffer so a tab that re-attaches (after the user
navigated elsewhere in the app) shows the recent screen again.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import glob
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from laya.claude_code import store
from laya.config import get_agent_binary
from laya.db.sqlite import get_db

log = structlog.get_logger()

# Bytes of recent output kept per session for re-attaching tabs.
BUFFER_LIMIT = 200 * 1024
# Closed sessions offered for resuming.
CLOSED_LIST_LIMIT = 20
# Seconds between SIGHUP/SIGTERM and SIGKILL when a session is closed.
KILL_GRACE_SECONDS = 2.0

DEFAULT_COLS = 120
DEFAULT_ROWS = 32

BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"

# C0 and C1 control characters plus DEL. Removing them from referenced text
# keeps it from submitting the prompt (CR/LF), ending the bracketed paste early
# (ESC) or sending keystrokes such as Ctrl-C.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Unicode line and paragraph separators render as line breaks in some terminals.
_LINE_SEPARATORS = re.compile(r"[  ]")

MCP_UNAVAILABLE_NOTE = (
    "Laya's MCP server is not available, so this session cannot read Laya's cards, "
    "board or My work."
)


class TerminalError(Exception):
    """A request the terminal module refuses; ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class TerminalSession:
    id: str
    name: str
    cwd: str
    claude_session_id: str
    created_at: str
    process: subprocess.Popen | None = None
    master_fd: int | None = None
    mcp_token: str | None = None
    mcp_config_path: str | None = None
    note: str | None = None
    exit_code: int | None = None
    buffer: bytearray = field(default_factory=bytearray)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    pending_write: bytearray = field(default_factory=bytearray)
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS

    @property
    def running(self) -> bool:
        return self.process is not None and self.exit_code is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cwd": self.cwd,
            "claude_session_id": self.claude_session_id,
            "created_at": self.created_at,
            "running": self.running,
            "exit_code": self.exit_code,
            "pid": self.process.pid if self.process else None,
            "note": self.note,
        }


# Live sessions of this engine process, by Laya session id.
_sessions: dict[str, TerminalSession] = {}


# ── table ────────────────────────────────────────────────────────────────


async def _db():
    db = await get_db()
    await store.ensure_tables(db)
    return db


async def startup() -> None:
    """Mark sessions a previous engine process left open as closed.

    Their processes ended with that engine, so the rows are only useful for
    resuming.
    """
    db = await _db()
    await db.execute(
        "UPDATE claude_code_terminal_sessions SET closed_at = ? WHERE closed_at IS NULL",
        (store.now_iso(),),
    )
    await db.commit()


async def _insert_row(s: TerminalSession) -> None:
    db = await _db()
    await db.execute(
        """INSERT INTO claude_code_terminal_sessions
               (id, name, cwd, claude_session_id, created_at, last_active_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(id) DO UPDATE SET
               name = excluded.name, cwd = excluded.cwd,
               last_active_at = excluded.last_active_at, closed_at = NULL""",
        (s.id, s.name, s.cwd, s.claude_session_id, s.created_at, store.now_iso()),
    )
    await db.commit()


async def _update_row(session_id: str, **fields: Any) -> None:
    if not fields:
        return
    db = await _db()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE claude_code_terminal_sessions SET {assignments} WHERE id = ?",
        (*fields.values(), session_id),
    )
    await db.commit()


async def _get_row(session_id: str) -> dict[str, Any] | None:
    db = await _db()
    rows = await db.execute_fetchall(
        "SELECT * FROM claude_code_terminal_sessions WHERE id = ?", (session_id,)
    )
    return dict(rows[0]) if rows else None


def _claude_config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def has_transcript(claude_session_id: str) -> bool:
    """True when Claude Code stored a conversation under this id (it can be resumed).

    Claude Code writes a transcript only once the conversation has a message,
    so a session that was opened and closed without one cannot be resumed.
    """
    pattern = str(_claude_config_dir() / "projects" / "*" / f"{glob.escape(claude_session_id)}.jsonl")
    return bool(glob.glob(pattern))


async def list_closed(limit: int = CLOSED_LIST_LIMIT) -> list[dict[str, Any]]:
    db = await _db()
    rows = await db.execute_fetchall(
        """SELECT * FROM claude_code_terminal_sessions
           WHERE closed_at IS NOT NULL
           ORDER BY COALESCE(last_active_at, created_at) DESC LIMIT ?""",
        (limit,),
    )
    out = []
    for r in rows:
        row = dict(r)
        if row["id"] in _sessions:
            continue
        row["resumable"] = has_transcript(row["claude_session_id"])
        out.append(row)
    return out


# ── defaults ─────────────────────────────────────────────────────────────


def default_cwd() -> str:
    somnia = Path.home() / "git" / "somnia"
    return str(somnia) if somnia.is_dir() else str(Path.home())


def _resolve_cwd(cwd: str | None) -> str:
    path = Path(os.path.expanduser((cwd or "").strip() or default_cwd()))
    if not path.is_absolute():
        raise TerminalError(f"Working directory must be an absolute path: {cwd}")
    if not path.is_dir():
        raise TerminalError(f"Working directory does not exist: {path}")
    return str(path.resolve())


def _clean_name(name: str | None, fallback: str) -> str:
    cleaned = _CONTROL_CHARS.sub("", (name or "")).strip()[:80]
    return cleaned or fallback


def sanitize_paste(text: str) -> bytes:
    """Text for a bracketed paste: one line, no control characters.

    Line breaks become spaces (so a multi-line reference stays readable on one
    line) and every other control character is removed.
    """
    one_line = _LINE_SEPARATORS.sub(" ", text.replace("\r\n", " ").replace("\n", " ").replace("\r", " "))
    return _CONTROL_CHARS.sub("", one_line).encode("utf-8")


# ── MCP wiring ───────────────────────────────────────────────────────────


def _mint_mcp(session: TerminalSession) -> None:
    """Give the session a read-scope MCP token and a 0600 config file, or a note.

    The token has no expiry; ``close`` revokes it.
    """
    try:
        from laya.llm.agent_backend import _write_private_json, build_turn_mcp_config
        from laya.llm.tools.definitions import read_tool_names
        from laya.mcp import turn_tokens

        tools = read_tool_names()
        if not tools:
            session.note = MCP_UNAVAILABLE_NOTE
            return
        token = turn_tokens.mint(tools, ttl_seconds=None)
        session.mcp_token = token
        session.mcp_config_path = _write_private_json(build_turn_mcp_config(token, None))
    except Exception as e:  # MCP wiring is optional; the terminal still starts.
        log.warning("claude_terminal_mcp_unavailable", error=str(e))
        _release_mcp(session)
        session.note = MCP_UNAVAILABLE_NOTE


def _release_mcp(session: TerminalSession) -> None:
    if session.mcp_token:
        from laya.mcp import turn_tokens

        turn_tokens.revoke(session.mcp_token)
        session.mcp_token = None
    if session.mcp_config_path:
        with contextlib.suppress(OSError):
            os.unlink(session.mcp_config_path)
        session.mcp_config_path = None


# ── process ──────────────────────────────────────────────────────────────


def build_args(binary: str, claude_session_id: str, resume: bool, mcp_config_path: str | None) -> list[str]:
    args = [binary]
    args += ["--resume", claude_session_id] if resume else ["--session-id", claude_session_id]
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path]
    return args


def _spawn_env() -> dict[str, str]:
    from laya.config import _augmented_path

    env = dict(os.environ)
    env["PATH"] = _augmented_path()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # Set by a Claude Code process for its children. An engine started from a
    # Claude Code session would pass it on, and claude then refuses to start as
    # a nested session.
    env.pop("CLAUDECODE", None)
    return env


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _make_controlling_tty() -> None:  # runs in the child between fork and exec
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _start_process(session: TerminalSession, args: list[str]) -> None:
    master, slave = pty.openpty()
    try:
        _set_winsize(slave, session.rows, session.cols)
        # start_new_session puts claude in its own session and process group, so
        # closing the tab can signal the whole group; the preexec step makes the
        # PTY its controlling terminal (job control, /dev/tty for child tools).
        proc = subprocess.Popen(
            args,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=session.cwd,
            env=_spawn_env(),
            start_new_session=True,
            preexec_fn=_make_controlling_tty,
            close_fds=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    session.master_fd = master
    session.process = proc
    loop = asyncio.get_running_loop()
    loop.add_reader(master, _on_readable, session)


def _broadcast(session: TerminalSession, item: bytes | dict | None) -> None:
    for q in list(session.subscribers):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(item)


def _append_output(session: TerminalSession, data: bytes) -> None:
    session.buffer += data
    overflow = len(session.buffer) - BUFFER_LIMIT
    if overflow > 0:
        del session.buffer[:overflow]
    _broadcast(session, data)


def _on_readable(session: TerminalSession) -> None:
    fd = session.master_fd
    if fd is None:
        return
    try:
        data = os.read(fd, 65536)
    except BlockingIOError:
        return
    except OSError as e:
        # EIO: the child closed its side of the PTY (it exited).
        if e.errno not in (errno.EIO, errno.EBADF):
            log.warning("claude_terminal_read_error", session_id=session.id, error=str(e))
        data = b""
    if data:
        _append_output(session, data)
        return
    _detach_fd(session)
    asyncio.get_running_loop().create_task(_reap(session))


def _detach_fd(session: TerminalSession) -> None:
    fd = session.master_fd
    if fd is None:
        return
    loop = asyncio.get_running_loop()
    loop.remove_reader(fd)
    loop.remove_writer(fd)
    session.master_fd = None
    with contextlib.suppress(OSError):
        os.close(fd)


async def _reap(session: TerminalSession) -> None:
    proc = session.process
    if proc is None or session.exit_code is not None:
        return
    code = await asyncio.to_thread(proc.wait)
    session.exit_code = code
    # The process is gone; its MCP token has no further use.
    _release_mcp(session)
    _broadcast(session, {"type": "exit", "code": code})
    log.info("claude_terminal_exited", session_id=session.id, code=code)


def _flush_pending(session: TerminalSession) -> None:
    fd = session.master_fd
    if fd is None:
        session.pending_write.clear()
        return
    loop = asyncio.get_running_loop()
    while session.pending_write:
        try:
            n = os.write(fd, session.pending_write)
        except BlockingIOError:
            loop.add_writer(fd, _flush_pending, session)
            return
        except OSError as e:
            log.warning("claude_terminal_write_error", session_id=session.id, error=str(e))
            session.pending_write.clear()
            break
        del session.pending_write[:n]
    loop.remove_writer(fd)


def write(session: TerminalSession, data: bytes) -> None:
    """Queue bytes for the PTY (keystrokes from the UI, or a paste)."""
    if not data or session.master_fd is None:
        return
    waiting = bool(session.pending_write)
    session.pending_write += data
    if not waiting:
        _flush_pending(session)


# ── public operations ────────────────────────────────────────────────────


def get(session_id: str) -> TerminalSession:
    session = _sessions.get(session_id)
    if session is None:
        raise TerminalError("Terminal session not found", status=404)
    return session


def list_open() -> list[dict[str, Any]]:
    return [s.to_dict() for s in sorted(_sessions.values(), key=lambda s: s.created_at)]


async def create(
    cwd: str | None = None,
    name: str | None = None,
    resume: str | None = None,
    cols: int | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    """Start ``claude`` in a new PTY, or resume a closed session by its Laya id."""
    if resume:
        if resume in _sessions:
            raise TerminalError("That session is already open", status=409)
        row = await _get_row(resume)
        if row is None:
            raise TerminalError("Terminal session not found", status=404)
        session = TerminalSession(
            id=row["id"],
            name=_clean_name(name, row["name"]),
            cwd=_resolve_cwd(cwd or row["cwd"]),
            claude_session_id=row["claude_session_id"],
            created_at=row["created_at"],
        )
    else:
        resolved = _resolve_cwd(cwd)
        session = TerminalSession(
            id=uuid.uuid4().hex,
            name=_clean_name(name, Path(resolved).name or "Claude"),
            cwd=resolved,
            claude_session_id=str(uuid.uuid4()),
            created_at=store.now_iso(),
        )
    if cols and rows:
        session.cols, session.rows = _clamp_size(cols, rows)

    _mint_mcp(session)
    args = build_args(get_agent_binary("claude_code"), session.claude_session_id, bool(resume),
                      session.mcp_config_path)
    try:
        _start_process(session, args)
    except (OSError, subprocess.SubprocessError) as e:
        _release_mcp(session)
        raise TerminalError(f"Could not start claude ({args[0]}): {e}", status=500) from e

    _sessions[session.id] = session
    await _insert_row(session)
    log.info("claude_terminal_started", session_id=session.id, cwd=session.cwd,
             resume=bool(resume), mcp=session.mcp_token is not None)
    return session.to_dict()


async def rename(session_id: str, name: str) -> dict[str, Any]:
    session = get(session_id)
    session.name = _clean_name(name, session.name)
    await _update_row(session_id, name=session.name)
    return session.to_dict()


def _clamp_size(cols: int, rows: int) -> tuple[int, int]:
    return max(10, min(int(cols), 1000)), max(4, min(int(rows), 500))


def resize(session_id: str, cols: int, rows: int) -> dict[str, Any]:
    session = get(session_id)
    session.cols, session.rows = _clamp_size(cols, rows)
    if session.master_fd is not None:
        with contextlib.suppress(OSError):
            _set_winsize(session.master_fd, session.rows, session.cols)
    return session.to_dict()


async def paste(session_id: str, text: str) -> dict[str, Any]:
    """Insert ``text`` at the prompt as a bracketed paste, without pressing Enter."""
    session = get(session_id)
    if not session.running:
        raise TerminalError("The session has ended", status=409)
    clean = sanitize_paste(text)
    if not clean.strip():
        raise TerminalError("Nothing to insert")
    write(session, BRACKETED_PASTE_START + clean + BRACKETED_PASTE_END)
    await _update_row(session_id, last_active_at=store.now_iso())
    return session.to_dict()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


async def _terminate(session: TerminalSession) -> None:
    proc = session.process
    if proc is not None and proc.poll() is None:
        _signal_group(proc, signal.SIGHUP)
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
            await asyncio.to_thread(proc.wait)
    if proc is not None and session.exit_code is None:
        session.exit_code = proc.returncode
    _detach_fd(session)
    _release_mcp(session)


async def close(session_id: str) -> None:
    """End the session: kill its process group, revoke its MCP token, keep the row."""
    session = get(session_id)
    _sessions.pop(session_id, None)
    await _terminate(session)
    _broadcast(session, None)
    await _update_row(session_id, closed_at=store.now_iso(), last_active_at=store.now_iso())
    log.info("claude_terminal_closed", session_id=session_id)


async def shutdown_all() -> None:
    """Close every session (engine shutdown)."""
    for session_id in list(_sessions):
        try:
            await close(session_id)
        except Exception as e:
            log.warning("claude_terminal_shutdown_error", session_id=session_id, error=str(e))


def subscribe(session: TerminalSession) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=4096)
    session.subscribers.add(q)
    return q


def unsubscribe(session: TerminalSession, q: asyncio.Queue) -> None:
    session.subscribers.discard(q)
