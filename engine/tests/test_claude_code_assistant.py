# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""The Claude terminal assistant: MCP server instructions and the `today` read tool."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from laya.api.mcp_api import register_mcp_transport
from laya.claude_code import assistant, store
from laya.db.timeutil import db_ts
from laya.llm.tools.definitions import (
    egress_tool_names,
    read_tool_names,
    select_tool_definitions,
    write_tool_names,
)
from laya.llm.tools.executor import execute_tool
from laya.mcp import turn_tokens
from laya.mcp.scope import scope_of
from tests.conftest import insert_test_card
from tests.test_mcp_turn_tokens import _open_session, _payload, _rpc, bearer_mode_all_scopes  # noqa: F401


# ── instructions ─────────────────────────────────────────────────────────

def test_instructions_are_short_and_name_the_rules_and_tools():
    text = assistant.server_instructions()
    assert text
    assert len(text.split()) <= 380
    for needle in ("today", "search_cards", "get_entity", "search_events", "search_documents",
                   "URL inline", "live", "Meetings are fixed", "Drafts only",
                   "somnia-chain", "somnia-lend", "agent ~"):
        assert needle in text, needle
    # Every tool the instructions name exists in the read scope.
    for name in ("today", "search_cards", "get_card", "search_entities", "get_entity",
                 "get_cards_by_entity", "search_events", "semantic_search", "search_documents",
                 "read_document", "get_recent_activity", "get_daily_summary", "get_omni_summary"):
        assert name in read_tool_names(), name


def test_instructions_missing_file_gives_none(monkeypatch, tmp_path):
    monkeypatch.setattr(assistant, "INSTRUCTIONS_FILE", tmp_path / "nope.md")
    assert assistant.server_instructions() is None


def test_mcp_initialize_serves_the_instructions(bearer_mode_all_scopes):  # noqa: F811
    app = FastAPI()
    register_mcp_transport(app)
    tok = turn_tokens.mint(read_tool_names(), ttl_seconds=60)
    try:
        with TestClient(app) as client:
            resp = _rpc(client, tok, "initialize", {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            })
            result = _payload(resp)["result"]
            assert result["instructions"] == assistant.server_instructions()
    finally:
        turn_tokens.revoke(tok)


# ── scope ────────────────────────────────────────────────────────────────

def test_today_is_a_read_tool_and_mcp_only():
    assert scope_of("today") == "read"
    assert "today" not in write_tool_names() | egress_tool_names()
    # Laya's own chat does not carry it (its toolset is token-budgeted).
    assert "today" not in {d["function"]["name"] for d in select_tool_definitions("what is today")}


def test_turn_token_reaches_today_but_not_write_tools(bearer_mode_all_scopes, db):  # noqa: F811
    app = FastAPI()
    register_mcp_transport(app)
    write = sorted(write_tool_names())[0]
    tok = turn_tokens.mint(read_tool_names() | {write}, ttl_seconds=60)
    try:
        with TestClient(app) as client:
            session = _open_session(client, tok)
            names = {t["name"] for t in _payload(_rpc(client, tok, "tools/list", {}, session, rid=2))["result"]["tools"]}
            assert "today" in names
            assert write not in names
            called = _payload(_rpc(client, tok, "tools/call", {"name": write, "arguments": {}}, session, rid=3))
            assert "error" in called or called["result"].get("isError")
    finally:
        turn_tokens.revoke(tok)


# ── the today tool ───────────────────────────────────────────────────────

async def _tag(db, card_id: str, name: str) -> None:
    await db.execute("INSERT OR IGNORE INTO tags (name, is_system) VALUES (?, 1)", (name,))
    await db.execute(
        "INSERT INTO tag_assignments (tag_id, target_type, target_id) "
        "SELECT tag_id, 'card', ? FROM tags WHERE name = ?", (card_id, name),
    )


@pytest.mark.asyncio
async def test_today_shape_lanes_links_effort_and_recheck(db):
    now = datetime.now(timezone.utc)
    await insert_test_card(db, card_id="c_run", event_id="e1", status="agent_running", priority="LOW",
                           entity_id="github:pull_request:somnia-chain/x#1")
    await insert_test_card(db, card_id="c_todo", event_id="e2", status="ready", priority="HIGH",
                           entity_id="linear:issue:TRAD-1")
    await insert_test_card(db, card_id="c_crit", event_id="e3", status="ready", priority="CRITICAL",
                           entity_id="slack:thread:C1:1")
    await insert_test_card(db, card_id="c_low", event_id="e4", status="ready", priority="LOW",
                           entity_id="gmail:thread:t1")
    await insert_test_card(db, card_id="c_done", event_id="e5", status="done", entity_id="linear:issue:TRAD-2")
    await insert_test_card(db, card_id="c_old_done", event_id="e6", status="done", entity_id="linear:issue:TRAD-3")
    await insert_test_card(db, card_id="c_gone", event_id="e7", status="dismissed", entity_id="linear:issue:TRAD-4")
    await _tag(db, "c_todo", "lane:todo")
    await db.execute("UPDATE action_cards SET source_url = 'https://linear.app/x/issue/TRAD-1', "
                     "staged_output = ? WHERE card_id = 'c_todo'",
                     (json.dumps({"effort": {"you_minutes": 30, "agent_minutes": 120}}),))
    await db.execute("UPDATE action_cards SET resolved_at = ?, status_set_by = 'laya', "
                     "status_reason = 'merged' WHERE card_id = 'c_done'", (db_ts(now),))
    await db.execute("UPDATE action_cards SET resolved_at = ? WHERE card_id = 'c_old_done'",
                     (db_ts(now - timedelta(days=3)),))
    # The Slack thread was last active 3 days ago: due for a re-check.
    await db.execute("UPDATE events SET timestamp = ? WHERE event_id = 'e3'", (db_ts(now - timedelta(days=3)),))
    await db.execute("UPDATE events SET timestamp = ? WHERE event_id != 'e3'", (db_ts(now - timedelta(hours=1)),))
    await db.commit()

    out = json.loads(await execute_tool("today", {}))

    assert set(out) == {"now", "stale_after_hours", "board", "my_work", "closed_today", "recheck"}
    board = out["board"]
    assert [c["card_id"] for c in board["in_progress"]] == ["c_run"]
    assert [c["card_id"] for c in board["todo"]] == ["c_todo"]
    assert [c["card_id"] for c in board["backlog_top"]] == ["c_crit", "c_low"]
    assert board["counts"] == {"in_progress": 1, "todo": 1, "backlog": 2}
    todo = board["todo"][0]
    assert todo["link"] == "https://linear.app/x/issue/TRAD-1"
    assert todo["effort"] == {"you": "30m", "agent": "2h"}
    assert todo["platform"] == "linear"
    assert todo["last_activity"]

    assert [c["card_id"] for c in out["closed_today"]] == ["c_done"]
    assert out["closed_today"][0]["closed_by"] == "laya"
    assert [r["card_id"] for r in out["recheck"]] == ["c_crit"]
    assert out["recheck"][0]["idle_hours"] >= 71

    # No My work cache yet: reported as such, never fetched.
    assert out["my_work"] == {"linear": {"cached": False}, "github": {"cached": False}}


@pytest.mark.asyncio
async def test_today_reads_my_work_from_the_cache_only(db, monkeypatch):
    from laya.claude_code import my_work

    async def boom(*a, **k):
        raise AssertionError("today must not fetch My work")

    monkeypatch.setattr(my_work, "fetch_linear", boom)
    monkeypatch.setattr(my_work, "fetch_github", boom)
    fetched = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()

    def pr(n, **kw):
        base = {"repo": "somnia-chain/x", "number": n, "title": f"PR {n}", "url": f"https://github.com/somnia-chain/x/pull/{n}",
                "draft": False, "conflicts": False, "review_decision": None, "approvals": 0,
                "changes_requested": 0, "unresolved_threads": 0, "requested_reviewers": 1,
                "checks": {"state": "success", "failing": 0, "pending": 0, "total": 3},
                "updated_at": "2026-11-19T10:00:00Z", "linear_key": None}
        return {**base, **kw}

    prs = [pr(1, checks={"state": "failure", "failing": 2, "pending": 0, "total": 3}),
           pr(2), pr(3, review_decision="APPROVED", approvals=1), pr(4, draft=True)]
    issues = [
        {"identifier": "TRAD-1", "title": "a", "url": "https://linear.app/i/TRAD-1", "state": "In Progress",
         "state_type": "started", "priority": 2, "assigned_to_me": True, "created_by_me": False},
        {"identifier": "TRAD-2", "title": "b", "url": "https://linear.app/i/TRAD-2", "state": "Todo",
         "state_type": "unstarted", "priority": 1, "assigned_to_me": True, "created_by_me": True},
        {"identifier": "TRAD-3", "title": "c", "url": "https://linear.app/i/TRAD-3", "state": "Todo",
         "state_type": "unstarted", "priority": 0, "assigned_to_me": True, "created_by_me": False},
        {"identifier": "TRAD-4", "title": "d", "url": "https://linear.app/i/TRAD-4", "state": "Backlog",
         "state_type": "backlog", "priority": 3, "assigned_to_me": False, "created_by_me": True},
    ]
    await store.set_state("my_work", "github", json.dumps({"items": prs, "fetched_at": fetched}))
    await store.set_state("my_work", "linear", json.dumps({"items": issues, "fetched_at": fetched}))

    out = await assistant.today()
    gh = out["my_work"]["github"]
    assert gh["cached"] is True and 89 <= gh["age_minutes"] <= 91
    assert [p["pr"] for p in gh["needs_action"]] == ["somnia-chain/x#1"]
    assert "2 checks failing" in gh["needs_action"][0]["state"]
    assert [p["pr"] for p in gh["waiting"]] == ["somnia-chain/x#2"]
    assert [p["pr"] for p in gh["ready_to_merge"]] == ["somnia-chain/x#3"]
    assert [p["pr"] for p in gh["drafts"]] == ["somnia-chain/x#4"]
    assert gh["ready_to_merge"][0]["link"] == "https://github.com/somnia-chain/x/pull/3"

    lin = out["my_work"]["linear"]
    assert [i["id"] for i in lin["assigned"]["in_progress"]] == ["TRAD-1"]
    assert [i["id"] for i in lin["assigned"]["todo"]] == ["TRAD-2", "TRAD-3"]
    assert lin["assigned"]["todo"][0]["priority"] == "urgent"
    assert "backlog" not in lin["assigned"]
    assert lin["created_by_me_open"] == 1


@pytest.mark.asyncio
async def test_card_tools_return_the_source_link(db):
    await insert_test_card(db, card_id="c1", event_id="e1", status="ready", header="Oracle voided questions",
                           entity_id="slack:thread:C1:1")
    await db.execute("UPDATE action_cards SET source_url = 'https://slack.example/p1' WHERE card_id = 'c1'")
    await db.commit()
    card = json.loads(await execute_tool("get_card", {"card_id": "c1"}))["card"]
    assert card["source_url"] == "https://slack.example/p1"
    found = json.loads(await execute_tool("search_cards", {"query": "Oracle", "semantic": False}))
    assert found["groups"][0]["cards"][0]["source_url"] == "https://slack.example/p1"
    by_entity = json.loads(await execute_tool("get_cards_by_entity", {"entity_id": "slack:thread:C1:1"}))
    assert by_entity["cards"][0]["source_url"] == "https://slack.example/p1"
