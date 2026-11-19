# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Claude terminal sessions (`laya/claude_code/terminal.py`) and their routes.

A small shell script stands in for the `claude` binary: it prints its
arguments and then runs `cat`, so the tests see what the session was started
with and what reaches the PTY.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import stat
import struct
import termios

import anyio
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from laya.claude_code import terminal
from laya.mcp import turn_tokens
from laya.security import keychain
from laya.security.engine_auth import WS_SUBPROTOCOL
from laya.security.keychain import ENGINE_TOKEN_ENV, store_engine_token

FORBIDDEN_FLAGS = ("--setting-sources", "--permission-mode", "--agents", "--system-prompt",
                   "--append-system-prompt", "--dangerously-skip-permissions", "-p")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake-claude"
    script.write_text('#!/bin/sh\necho "ARGS: $*"\nexec cat\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(terminal, "get_agent_binary", lambda _agent: str(script))
    return script


@pytest.fixture(autouse=True)
def _kill_leftover_sessions():
    """Kill whatever a failed test left running (synchronously, outside any loop)."""
    yield
    for session in list(terminal._sessions.values()):
        if session.process and session.process.poll() is None:
            os.killpg(session.process.pid, signal.SIGKILL)
            session.process.wait()
        if session.master_fd is not None:
            os.close(session.master_fd)
        terminal._release_mcp(session)
    terminal._sessions.clear()


async def _wait_for(session: terminal.TerminalSession, needle: bytes, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while needle not in session.buffer:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{needle!r} not in output: {bytes(session.buffer)!r}")
        await asyncio.sleep(0.02)


# ── pure helpers ─────────────────────────────────────────────────────────


def test_sanitize_paste_keeps_one_line_without_control_characters():
    text = "[TRAD-1] Fix it\r\nnow\x1b[201~\x03\x04\ttab\x7f\x9b end x"
    out = terminal.sanitize_paste(text)
    assert out == "[TRAD-1] Fix it now[201~tab end x".encode()
    assert not any(b < 0x20 or b == 0x7F for b in out)


def test_build_args_adds_only_session_id_and_mcp_config():
    args = terminal.build_args("/bin/claude", "abc", resume=False, mcp_config_path="/tmp/x.json")
    assert args == ["/bin/claude", "--session-id", "abc", "--mcp-config", "/tmp/x.json"]
    resumed = terminal.build_args("/bin/claude", "abc", resume=True, mcp_config_path=None)
    assert resumed == ["/bin/claude", "--resume", "abc"]
    assert not set(FORBIDDEN_FLAGS) & set(args + resumed)


def test_default_cwd_is_an_existing_directory():
    assert os.path.isdir(terminal.default_cwd())


# ── module ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_list_paste_resize_rename_close(db, fake_claude, tmp_path, monkeypatch):
    info = await terminal.create(cwd=str(tmp_path), name="work\n tab", cols=100, rows=30)
    session = terminal.get(info["id"])
    assert info["running"] and info["name"] == "work tab" and info["cwd"] == str(tmp_path.resolve())
    await _wait_for(session, b"ARGS:")
    out = bytes(session.buffer).decode()
    assert f"--session-id {session.claude_session_id}" in out
    assert "--mcp-config" in out
    assert not any(f" {flag} " in f" {out} " for flag in FORBIDDEN_FLAGS)

    # The MCP config is private and carries a live read-only token without expiry.
    path = session.mcp_config_path
    assert path and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    config = json.loads(open(path).read())["mcpServers"]["laya"]
    assert config["type"] == "http" and config["url"].endswith("/mcp/")
    token = config["headers"]["Authorization"].removeprefix("Bearer ")
    allowed = turn_tokens.lookup(token)
    from laya.llm.tools.definitions import read_tool_names, write_tool_names

    assert allowed and allowed == frozenset(read_tool_names())
    assert not allowed & write_tool_names()

    assert [s["id"] for s in terminal.list_open()] == [session.id]

    # A paste is bracketed, one line, and never followed by Enter.
    written: list[bytes] = []
    real_write = terminal.write
    monkeypatch.setattr(terminal, "write", lambda s, data: (written.append(data), real_write(s, data)))
    await terminal.paste(session.id, "[TRAD-7] Title\n— https://x\r\x1b[201~\x03")
    assert written == [b"\x1b[200~[TRAD-7] Title \xe2\x80\x94 https://x [201~\x1b[201~"]
    await _wait_for(session, b"[TRAD-7] Title")
    with pytest.raises(terminal.TerminalError):
        await terminal.paste(session.id, "\n\r\x03")

    terminal.resize(session.id, 132, 40)
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(session.master_fd, termios.TIOCGWINSZ, b"\0" * 8))
    assert (cols, rows) == (132, 40)

    await terminal.rename(session.id, "renamed")
    assert (await terminal._get_row(session.id))["name"] == "renamed"

    proc = session.process
    await terminal.close(session.id)
    assert proc.poll() is not None
    assert turn_tokens.lookup(token) is None
    assert not os.path.exists(path)
    assert terminal.list_open() == []
    with pytest.raises(terminal.TerminalError):
        terminal.get(session.id)
    row = await terminal._get_row(session.id)
    assert row["closed_at"] is not None

    closed = await terminal.list_closed()
    assert [c["id"] for c in closed] == [session.id] and closed[0]["resumable"] is False
    transcript = tmp_path / "claude-home" / "projects" / "-x" / f"{session.claude_session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    assert (await terminal.list_closed())[0]["resumable"] is True


@pytest.mark.asyncio
async def test_resume_reopens_the_row_with_claude_resume(db, fake_claude, tmp_path):
    first = await terminal.create(cwd=str(tmp_path))
    await terminal.close(first["id"])
    again = await terminal.create(resume=first["id"])
    session = terminal.get(again["id"])
    assert again["id"] == first["id"] and again["claude_session_id"] == first["claude_session_id"]
    await _wait_for(session, b"ARGS:")
    assert f"--resume {first['claude_session_id']}".encode() in session.buffer
    with pytest.raises(terminal.TerminalError) as e:
        await terminal.create(resume=first["id"])
    assert e.value.status == 409
    assert (await terminal._get_row(first["id"]))["closed_at"] is None


@pytest.mark.asyncio
async def test_process_exit_is_reported_and_releases_the_token(db, fake_claude, tmp_path):
    info = await terminal.create(cwd=str(tmp_path))
    session = terminal.get(info["id"])
    queue = terminal.subscribe(session)
    token = session.mcp_token
    terminal.write(session, b"\x04")  # EOF ends `cat`
    for _ in range(200):
        if session.exit_code is not None:
            break
        await asyncio.sleep(0.02)
    assert session.exit_code == 0 and not session.running
    assert turn_tokens.lookup(token) is None
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert {"type": "exit", "code": 0} in items
    with pytest.raises(terminal.TerminalError):
        await terminal.paste(session.id, "late")


@pytest.mark.asyncio
async def test_without_laya_mcp_claude_starts_plain_with_a_note(db, fake_claude, tmp_path, monkeypatch):
    import laya.llm.tools.definitions as definitions

    monkeypatch.setattr(definitions, "read_tool_names", lambda: set())
    info = await terminal.create(cwd=str(tmp_path))
    session = terminal.get(info["id"])
    assert info["note"] == terminal.MCP_UNAVAILABLE_NOTE and session.mcp_token is None
    await _wait_for(session, b"ARGS:")
    assert b"--mcp-config" not in session.buffer


@pytest.mark.asyncio
async def test_create_refuses_a_missing_directory(db, fake_claude, tmp_path):
    with pytest.raises(terminal.TerminalError) as e:
        await terminal.create(cwd=str(tmp_path / "nope"))
    assert e.value.status == 422
    assert terminal.list_open() == []


# ── REST ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_routes(db, fake_claude, tmp_path):
    from laya.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/claude-code/terminal/sessions", json={"cwd": str(tmp_path), "name": "t"})
        assert resp.status_code == 200
        sid = resp.json()["id"]
        listing = (await client.get("/claude-code/terminal/sessions")).json()
        assert [s["id"] for s in listing["sessions"]] == [sid] and listing["default_cwd"]

        assert (await client.post(f"/claude-code/terminal/{sid}/input", json={"text": "hi"})).status_code == 200
        assert (await client.post(f"/claude-code/terminal/{sid}/input", json={"text": 3})).status_code == 422
        assert (await client.post("/claude-code/terminal/nope/input", json={"text": "x"})).status_code == 404
        assert (await client.post(f"/claude-code/terminal/{sid}/resize", json={"cols": 90, "rows": 20})).status_code == 200
        resp = await client.patch(f"/claude-code/terminal/sessions/{sid}", json={"name": "new"})
        assert resp.json()["name"] == "new"
        assert (await client.post("/claude-code/terminal/sessions", json={"cwd": 5})).status_code == 422

        assert (await client.delete(f"/claude-code/terminal/sessions/{sid}")).status_code == 200
        assert (await client.delete(f"/claude-code/terminal/sessions/{sid}")).status_code == 404
        listing = (await client.get("/claude-code/terminal/sessions")).json()
        assert listing["sessions"] == [] and [c["id"] for c in listing["closed"]] == [sid]


# ── websocket ────────────────────────────────────────────────────────────

TOKEN = "lyae_terminal_test_token_1234567890"


@pytest.fixture
def real_auth(monkeypatch):
    """Undo the suite-wide auth bypass and install a known app token."""
    from laya.main import app
    from laya.security.engine_auth import require_engine_auth, require_ws_engine_auth

    app.dependency_overrides.pop(require_engine_auth, None)
    app.dependency_overrides.pop(require_ws_engine_auth, None)
    monkeypatch.setattr(keychain, "_process_engine_token", None)
    monkeypatch.delenv(ENGINE_TOKEN_ENV, raising=False)
    store_engine_token(TOKEN)
    return app


def _refused(app, subprotocols, origin="tauri://localhost") -> bool:
    try:
        with TestClient(app).websocket_connect(
            "/claude-code/terminal/any/pty", subprotocols=subprotocols, headers={"origin": origin},
        ):
            return False
    except WebSocketDisconnect:
        return True


def test_pty_socket_needs_the_app_token(real_auth):
    app = real_auth
    assert _refused(app, None)
    assert _refused(app, [WS_SUBPROTOCOL])
    assert _refused(app, [WS_SUBPROTOCOL, "wrong-token"])
    assert _refused(app, [WS_SUBPROTOCOL, TOKEN], origin="https://evil.example")
    # With the token the handshake is accepted; an unknown session closes with 4404.
    with TestClient(app).websocket_connect(
        "/claude-code/terminal/any/pty", subprotocols=[WS_SUBPROTOCOL, TOKEN],
        headers={"origin": "tauri://localhost"},
    ) as ws:
        assert ws.accepted_subprotocol == WS_SUBPROTOCOL
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
        assert e.value.code == 4404


def test_pty_rest_routes_need_the_app_token(real_auth):
    client = TestClient(real_auth)
    assert client.get("/claude-code/terminal/sessions").status_code == 401
    assert client.post("/claude-code/terminal/x/input", json={"text": "a"}).status_code == 401
    ok = client.post("/claude-code/terminal/x/input", json={"text": "a"},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 404


def test_pty_socket_streams_output_and_input(fake_claude, tmp_path, monkeypatch):
    """Create, attach, type, re-attach (buffered output), close — over one event loop."""
    from laya.main import app

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(terminal, "_insert_row", _noop)
    monkeypatch.setattr(terminal, "_update_row", _noop)
    client = TestClient(app)
    with anyio.from_thread.start_blocking_portal() as portal:
        # Every request runs on this one portal's loop, where the PTY reader lives.
        client.portal = portal
        sid = client.post("/claude-code/terminal/sessions", json={"cwd": str(tmp_path)}).json()["id"]

        def read_until(ws, needle: bytes) -> bytes:
            seen = b""
            while needle not in seen:
                msg = ws.receive()
                if msg.get("bytes"):
                    seen += msg["bytes"]
            return seen

        with client.websocket_connect(f"/claude-code/terminal/{sid}/pty") as ws:
            assert b"ARGS:" in read_until(ws, b"ARGS:")
            ws.send_text(json.dumps({"type": "resize", "cols": 101, "rows": 33}))
            ws.send_bytes(b"hello pty\n")
            read_until(ws, b"hello pty\r\nhello pty")
        assert terminal.get(sid).cols == 101

        # A re-attached socket first gets the buffered output.
        with client.websocket_connect(f"/claude-code/terminal/{sid}/pty") as ws:
            backlog = ws.receive_bytes()
            assert b"ARGS:" in backlog and b"hello pty" in backlog
            status = json.loads(ws.receive_text())
            assert status["type"] == "status" and status["running"] is True
            assert client.delete(f"/claude-code/terminal/sessions/{sid}").status_code == 200
            # Closing the session closes the attached socket.
            while (msg := ws.receive())["type"] != "websocket.close":
                pass
            assert msg["code"] == 1000
        client.portal = None
