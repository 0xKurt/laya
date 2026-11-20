# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""How the terminal starts each coding agent CLI (``terminal.py``).

A launch spec says, for one agent: the binary (``config.get_agent_binary``,
the same detection the agent adapters in ``laya.agents`` use), the arguments
for a new and a resumed session, how Laya's MCP server is attached with the
session's read token, and how the agent's own conversation id is known so the
session can be resumed later.

- ``claude_code``: ``claude --session-id <uuid>`` / ``--resume <uuid>`` and
  ``--mcp-config <0600 file>``. Claude Code reads the server's ``instructions``.
- ``codex_cli``: ``codex`` (or ``codex resume <thread id>``) with Laya's server
  added through ``-c mcp_servers.laya.*`` overrides; the token travels in an
  environment variable named by ``bearer_token_env_var`` (never in argv). The
  assistant instructions are passed codex-natively as ``developer_instructions``.
  Codex picks its own thread id, so it is looked up after the conversation
  starts (``resolve_codex_thread``). Sessions start with the read-only sandbox
  and on-request approvals.
- Other agents are listed as not supported in the terminal yet.

The agent travels in the session's stored conversation id: a Claude Code
session stores the bare uuid (as before), another agent stores
``<agent>:<id>`` (the id may be empty until it is known). No new column.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import structlog

log = structlog.get_logger()

CLAUDE = "claude_code"
CODEX = "codex_cli"

# Environment variable that carries the session's MCP token to codex.
CODEX_TOKEN_ENV = "LAYA_TERMINAL_MCP_TOKEN"

AGENT_LABELS = {
    "claude_code": "Claude Code",
    "codex_cli": "Codex",
    "gemini_cli": "Gemini CLI",
    "cursor_cli": "Cursor",
    "pi_cli": "Pi",
}

NOT_SUPPORTED_REASON = "Not supported in the terminal yet."


@dataclass
class Launch:
    """What ``terminal.py`` runs: argv, extra environment, files to delete on close."""

    args: list[str]
    env: dict[str, str] = field(default_factory=dict)


# ── conversation ids ─────────────────────────────────────────────────────


def encode_ref(agent: str, conversation_id: str) -> str:
    return conversation_id if agent == CLAUDE else f"{agent}:{conversation_id}"


def decode_ref(stored: str) -> tuple[str, str]:
    """``(agent, conversation id)`` from the stored value (bare uuid = Claude Code)."""
    agent, sep, rest = (stored or "").partition(":")
    if sep and agent in AGENT_LABELS:
        return agent, rest
    return CLAUDE, stored


# ── Claude Code ──────────────────────────────────────────────────────────


def claude_args(binary: str, session_id: str, resume: bool, mcp_config_path: str | None) -> list[str]:
    args = [binary]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path]
    return args


def _claude_config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def claude_has_transcript(session_id: str) -> bool:
    if not session_id:
        return False
    pattern = str(_claude_config_dir() / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
    return bool(glob.glob(pattern))


# ── Codex ────────────────────────────────────────────────────────────────


def _toml_str(value: str) -> str:
    # A JSON string is a valid TOML basic string for what json.dumps emits.
    return json.dumps(value, ensure_ascii=False)


def codex_args(
    binary: str,
    thread_id: str | None,
    mcp_url: str | None,
    instructions: str | None,
) -> list[str]:
    """``codex`` (new) or ``codex resume <thread_id>``, read-only, with Laya's MCP server."""
    args = [binary]
    if thread_id:
        args += ["resume", thread_id]
    # --no-daemon: the TUI runs its own server, so the token variable below
    # reaches it (a shared daemon would not see this process's environment).
    args += ["--no-daemon", "--sandbox", "read-only", "--ask-for-approval", "on-request"]
    if mcp_url:
        args += [
            "-c", f"mcp_servers.laya.url={_toml_str(mcp_url)}",
            "-c", f"mcp_servers.laya.bearer_token_env_var={_toml_str(CODEX_TOKEN_ENV)}",
        ]
    if instructions:
        args += ["-c", f"developer_instructions={_toml_str(instructions)}"]
    return args


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _iso_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def resolve_codex_thread(cwd: str, started_at: str, claimed: set[str]) -> str | None:
    """The codex thread a terminal session started, or None while it has none.

    Codex chooses its thread id itself and records each thread (with cwd and
    creation time) in its state database. The session's thread is the earliest
    one in the session's folder created after the session started that no other
    Laya session has claimed. Read-only, best effort: an unknown database
    layout means "not resumable", never an error.
    """
    try:
        since_ms = _iso_ms(started_at) - 2000
    except ValueError:
        return None
    for db_path in sorted(glob.glob(str(_codex_home() / "state_*.sqlite")), reverse=True):
        with contextlib.suppress(sqlite3.Error, OSError):
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            try:
                rows = conn.execute(
                    """SELECT id FROM threads
                       WHERE cwd = ? AND COALESCE(created_at_ms, created_at * 1000) >= ?
                         AND archived = 0
                       ORDER BY COALESCE(created_at_ms, created_at * 1000) ASC""",
                    (cwd, since_ms),
                ).fetchall()
            finally:
                conn.close()
            for (thread_id,) in rows:
                if thread_id and thread_id not in claimed:
                    return str(thread_id)
            return None
    return None


# ── registry ─────────────────────────────────────────────────────────────


def supported(agent: str) -> bool:
    return agent in (CLAUDE, CODEX)


def installed(agent: str, binary_lookup: Callable[[str], str]) -> bool:
    path = binary_lookup(agent)
    return bool(path) and os.path.isabs(path) and os.access(path, os.X_OK)


def agents(binary_lookup: Callable[[str], str]) -> list[dict]:
    """Every known agent with whether the terminal can start it, and why not."""
    out = []
    for agent, label in AGENT_LABELS.items():
        is_installed = installed(agent, binary_lookup)
        reason = None
        if not supported(agent):
            reason = NOT_SUPPORTED_REASON
        elif not is_installed:
            reason = f"{label} CLI is not installed."
        out.append({
            "id": agent,
            "label": label,
            "installed": is_installed,
            "supported": supported(agent),
            "available": reason is None,
            "reason": reason,
        })
    return out


def default_agent(configured: str | None, binary_lookup: Callable[[str], str]) -> str:
    """The configured coding agent when the terminal can start it, else the first
    one it can start (Claude Code when none is installed, so the error names it)."""
    if configured and supported(configured) and installed(configured, binary_lookup):
        return configured
    for agent in AGENT_LABELS:
        if supported(agent) and installed(agent, binary_lookup):
            return agent
    return CLAUDE
