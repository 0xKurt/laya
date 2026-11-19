# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for the short-lived MCP turn tokens used by agent chat / Coherence turns."""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from laya.api.mcp_api import register_mcp_transport
from laya.config import load_settings, save_settings
from laya.llm.tools.definitions import egress_tool_names, write_tool_names
from laya.mcp import turn_tokens
from laya.security.keychain import delete_mcp_token, store_mcp_token

ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def bearer_mode_all_scopes():
    """Bearer auth on with every scope enabled for external clients."""
    original = load_settings().get("mcp")
    s = load_settings()
    s["mcp"] = {"auth_mode": "bearer", "tool_scopes": {"read": True, "write": True, "egress": True}}
    save_settings(s)
    store_mcp_token("lyat_user_token")
    yield
    s = load_settings()
    if original is None:
        s.pop("mcp", None)
    else:
        s["mcp"] = original
    save_settings(s)
    delete_mcp_token()


def _rpc(client: TestClient, token: str, method: str, params: dict, session: str | None = None,
         rid: int = 1):
    headers = {**ACCEPT, "authorization": f"Bearer {token}"}
    if session:
        headers["mcp-session-id"] = session
    return client.post(
        "/mcp/", headers=headers,
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
    )


def _payload(resp) -> dict:
    import json

    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return resp.json()


def _open_session(client: TestClient, token: str) -> str:
    resp = _rpc(client, token, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert resp.status_code == 200, resp.text
    session = resp.headers["mcp-session-id"]
    client.post("/mcp/", headers={**ACCEPT, "authorization": f"Bearer {token}",
                                  "mcp-session-id": session},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return session


def test_mint_keeps_only_read_tools():
    write = sorted(write_tool_names())[0]
    egress = sorted(egress_tool_names())[0]
    tok = turn_tokens.mint({"search_cards", write, egress}, ttl_seconds=60)
    try:
        assert turn_tokens.lookup(tok) == frozenset({"search_cards"})
        assert turn_tokens.lookup_header(f"Bearer {tok}") == frozenset({"search_cards"})
    finally:
        turn_tokens.revoke(tok)
    assert turn_tokens.lookup(tok) is None


def test_expired_and_unknown_tokens_are_rejected(monkeypatch):
    tok = turn_tokens.mint({"search_cards"}, ttl_seconds=5)
    real = time.monotonic
    monkeypatch.setattr(turn_tokens.time, "monotonic", lambda: real() + 10)
    assert turn_tokens.lookup(tok) is None
    assert turn_tokens.lookup("lyat_turn_unknown") is None
    assert turn_tokens.lookup("lyat_user_token") is None


def test_turn_token_session_lists_only_its_tools(bearer_mode_all_scopes):
    app = FastAPI()
    register_mcp_transport(app)
    write = sorted(write_tool_names())[0]
    tok = turn_tokens.mint({"search_cards", "get_card", write}, ttl_seconds=60)
    try:
        with TestClient(app) as client:
            session = _open_session(client, tok)
            listed = _payload(_rpc(client, tok, "tools/list", {}, session, rid=2))
            names = {t["name"] for t in listed["result"]["tools"]}
            assert names == {"search_cards", "get_card"}

            called = _payload(_rpc(client, tok, "tools/call",
                                   {"name": write, "arguments": {}}, session, rid=3))
            assert "error" in called or called["result"].get("isError")

            # The user's own token still sees every enabled scope.
            user_session = _open_session(client, "lyat_user_token")
            user_listed = _payload(_rpc(client, "lyat_user_token", "tools/list", {},
                                        user_session, rid=4))
            assert write in {t["name"] for t in user_listed["result"]["tools"]}

            # A turn token cannot ride on a session opened with the user's token.
            resp = _rpc(client, tok, "tools/list", {}, user_session, rid=5)
            assert resp.status_code == 403
    finally:
        turn_tokens.revoke(tok)


def test_turn_token_does_not_open_legacy_sse(bearer_mode_all_scopes):
    app = FastAPI()
    register_mcp_transport(app)
    tok = turn_tokens.mint({"search_cards"}, ttl_seconds=60)
    try:
        resp = TestClient(app).get("/mcp/sse", headers={"authorization": f"Bearer {tok}"})
        assert resp.status_code == 401
    finally:
        turn_tokens.revoke(tok)


def test_revoked_turn_token_is_unauthorized(bearer_mode_all_scopes):
    app = FastAPI()
    register_mcp_transport(app)
    tok = turn_tokens.mint({"search_cards"}, ttl_seconds=60)
    turn_tokens.revoke(tok)
    resp = _rpc(TestClient(app), tok, "initialize", {})
    assert resp.status_code == 401
