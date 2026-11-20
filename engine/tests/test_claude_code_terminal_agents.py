# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Launch specs of the terminal's coding agents (`laya/claude_code/terminal_agents.py`)."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from laya.claude_code import terminal
from laya.claude_code import terminal_agents as agents


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    script = tmp_path / "fake-agent"
    script.write_text('#!/bin/sh\necho "ARGS: $*"\necho "TOKEN: ${LAYA_TERMINAL_MCP_TOKEN:-none}"\nexec cat\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(terminal, "get_agent_binary", lambda _agent: str(script))
    return script


@pytest.fixture(autouse=True)
def _kill_leftover_sessions():
    yield
    for session in list(terminal._sessions.values()):
        if session.process and session.process.poll() is None:
            os.killpg(session.process.pid, signal.SIGKILL)
            session.process.wait()
        if session.master_fd is not None:
            os.close(session.master_fd)
        terminal._release_mcp(session)
    terminal._sessions.clear()


async def _wait_for(session, needle: bytes, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while needle not in session.buffer:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{needle!r} not in output: {bytes(session.buffer)!r}")
        await asyncio.sleep(0.02)


def test_refs_keep_bare_claude_ids_and_prefix_other_agents():
    assert agents.encode_ref("claude_code", "u-1") == "u-1"
    assert agents.decode_ref("u-1") == ("claude_code", "u-1")
    assert agents.encode_ref("codex_cli", "t-1") == "codex_cli:t-1"
    assert agents.decode_ref("codex_cli:t-1") == ("codex_cli", "t-1")
    assert agents.decode_ref("codex_cli:") == ("codex_cli", "")
    # An unknown prefix is not an agent.
    assert agents.decode_ref("foo:bar") == ("claude_code", "foo:bar")


def test_claude_args():
    assert agents.claude_args("/b/claude", "id", False, "/t.json") == [
        "/b/claude", "--session-id", "id", "--mcp-config", "/t.json"]
    assert agents.claude_args("/b/claude", "id", True, None) == ["/b/claude", "--resume", "id"]


def test_codex_args_new_and_resume_read_only_token_not_in_argv():
    new = agents.codex_args("/b/codex", None, "http://127.0.0.1:1/mcp/", 'Be "calm".\nLinks.')
    assert new[:2] == ["/b/codex", "--no-daemon"]
    assert "--sandbox" in new and new[new.index("--sandbox") + 1] == "read-only"
    assert new[new.index("--ask-for-approval") + 1] == "on-request"
    assert 'mcp_servers.laya.url="http://127.0.0.1:1/mcp/"' in new
    assert f'mcp_servers.laya.bearer_token_env_var="{agents.CODEX_TOKEN_ENV}"' in new
    assert 'developer_instructions="Be \\"calm\\".\\nLinks."' in new
    resumed = agents.codex_args("/b/codex", "t-9", None, None)
    assert resumed[:3] == ["/b/codex", "resume", "t-9"]
    assert not any(a.startswith("mcp_servers") for a in resumed)


def test_agent_list_and_default(tmp_path):
    exe = tmp_path / "x"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    lookup = {"claude_code": str(exe), "codex_cli": str(exe)}.get
    listed = {a["id"]: a for a in agents.agents(lambda a: lookup(a) or "gemini")}
    assert listed["claude_code"]["available"] and listed["codex_cli"]["available"]
    assert not listed["gemini_cli"]["available"]
    assert listed["gemini_cli"]["reason"] == agents.NOT_SUPPORTED_REASON
    assert agents.default_agent("codex_cli", lambda a: lookup(a) or "") == "codex_cli"
    assert agents.default_agent("gemini_cli", lambda a: lookup(a) or "") == "claude_code"
    # Without Claude Code installed, the first available agent is the default.
    assert agents.default_agent(None, lambda a: str(exe) if a == "codex_cli" else "claude") == "codex_cli"
    # Configured codex that is not installed falls back to Claude Code.
    assert agents.default_agent("codex_cli", lambda a: str(exe) if a == "claude_code" else "codex") == "claude_code"


def _codex_state(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.execute("CREATE TABLE threads (id TEXT, cwd TEXT, created_at INTEGER, created_at_ms INTEGER, archived INTEGER)")
    conn.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, 0)", rows)
    conn.commit()
    conn.close()


def test_resolve_codex_thread_picks_earliest_unclaimed_in_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    start = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    _codex_state(tmp_path / "codex", [
        ("old", "/w", 0, ms(start - timedelta(minutes=5))),
        ("other-dir", "/x", 0, ms(start + timedelta(seconds=1))),
        ("a", "/w", 0, ms(start + timedelta(seconds=2))),
        ("b", "/w", 0, ms(start + timedelta(seconds=3))),
    ])
    assert agents.resolve_codex_thread("/w", start.isoformat(), set()) == "a"
    assert agents.resolve_codex_thread("/w", start.isoformat(), {"a"}) == "b"
    assert agents.resolve_codex_thread("/nowhere", start.isoformat(), set()) is None
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert agents.resolve_codex_thread("/w", start.isoformat(), set()) is None


@pytest.mark.asyncio
async def test_codex_session_gets_token_by_env_and_resumes_its_thread(db, fake_bin, tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "_check_agent", lambda _a: None)
    info = await terminal.create(cwd=str(tmp_path), agent="codex_cli")
    assert info["agent"] == "codex_cli" and info["claude_session_id"] == ""
    session = terminal.get(info["id"])
    await _wait_for(session, b"TOKEN:")
    out = bytes(session.buffer)
    assert b"--no-daemon" in out and b"--mcp-config" not in out
    if session.mcp_token:
        # The token reaches the process by environment, never by argv.
        assert f"TOKEN: {session.mcp_token}".encode() in out
        assert session.mcp_token.encode() not in out.split(b"TOKEN:")[0]
    # Codex created its thread; closing the session records it.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    created = datetime.fromisoformat(info["created_at"])
    _codex_state(tmp_path / "codex", [("thr-1", info["cwd"], 0, int(created.timestamp() * 1000) + 500)])
    await terminal.close(info["id"])
    closed = await terminal.list_closed()
    assert closed[0]["agent"] == "codex_cli" and closed[0]["resumable"] is True
    again = await terminal.create(resume=info["id"])
    session = terminal.get(again["id"])
    await _wait_for(session, b"ARGS:")
    assert b"resume thr-1" in session.buffer


@pytest.mark.asyncio
async def test_unsupported_agent_is_refused(db, fake_bin, tmp_path):
    with pytest.raises(terminal.TerminalError) as e:
        await terminal.create(cwd=str(tmp_path), agent="gemini_cli")
    assert "not supported" in str(e.value).lower()
