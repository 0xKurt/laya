# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Connect / disconnect / configure of the Claude Code connector, its service
loop, the re-check and the REST API."""

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import connect, discover, recheck, service, store
from laya.claude_code.discover import Discovery, PlatformDiscovery
from laya.db.timeutil import db_ts

@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    """No test of the connector may spawn `claude` or `gh`."""
    from laya.claude_code import cli as _cli

    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(_cli, "_run_process", side_effect=_refuse), \
         patch.object(_cli, "read_init", side_effect=_refuse), \
         patch.object(_cli, "cli_version", side_effect=_refuse), \
         patch.object(_cli, "run_gh", side_effect=_refuse):
        yield


SLACK_TOOLS = {"search": "mcp__s__search", "read_thread": "mcp__s__thread",
               "read_channel": "mcp__s__channel", "send": "mcp__s__send"}


def _discovery(**overrides) -> Discovery:
    platforms = {
        "slack": PlatformDiscovery(available=True, tools=dict(SLACK_TOOLS),
                                   identity={"slack_user_id": "U_ME", "name": "Kim", "email": "kim@work.com"}),
        "gmail": PlatformDiscovery(available=True, tools={"search": "g1", "get_thread": "g2"},
                                   identity={"email": "Kim@Work.com", "name": "Kim"}),
        "linear": PlatformDiscovery(available=False, problem="no Linear"),
        "github": PlatformDiscovery(available=True, tools={"cli": "gh"},
                                    identity={"github_login": "kim-gh", "name": "Kim", "email": ""}),
        "notion": PlatformDiscovery(available=True, tools={}, identity={}, problem="no identity"),
    }
    platforms.update(overrides)
    return Discovery(cli={"found": True, "path": "/bin/claude", "version": "2"}, platforms=platforms,
                     all_tools=list(SLACK_TOOLS.values()), at=1e12,
                     identity_read=True)


class Files:
    """In-memory team.json / rules.json."""

    def __init__(self, team=None, rules=None):
        self.team = team if team is not None else {"members": [
            {"name": "Mike", "email": "mike@work.com", "role": "manager", "notes": "",
             "aliases": [], "accounts": ["U_MIKE"]},
        ]}
        self.rules = rules if rules is not None else {"rules": [
            {"name": "Ignore bot messages", "enabled": True,
             "condition": {"field": "actor.email", "operator": "contains", "value": "bot"},
             "action": "drop"},
        ]}

    def patches(self):
        return [
            patch.object(connect, "load_team", side_effect=lambda: copy.deepcopy(self.team)),
            patch.object(connect, "save_team", side_effect=lambda t: setattr(self, "team", copy.deepcopy(t))),
            patch.object(connect, "load_rules", side_effect=lambda: copy.deepcopy(self.rules)),
            patch.object(connect, "save_rules", side_effect=lambda r: setattr(self, "rules", copy.deepcopy(r))),
        ]


@pytest.fixture
def files():
    f = Files()
    ps = f.patches()
    for p in ps:
        p.start()
    discover._cache = _discovery()
    yield f
    for p in ps:
        p.stop()
    discover.reset_cache()


def _self(team):
    return next(m for m in team["members"] if m["role"] == "self")


# ── team identity ────────────────────────────────────────────────────────


def test_merge_identity_creates_self_and_keeps_others():
    team = {"members": [{"name": "Mike", "email": "mike@work.com", "role": "manager"}]}
    out = connect.merge_identity_into_team(team, {"slack_user_id": "U_ME", "name": "Kim", "email": "kim@work.com"})
    me = _self(out)
    assert me["email"] == "kim@work.com" and me["accounts"] == ["U_ME"] and me["aliases"] == []
    assert out["members"][0]["name"] == "Mike" and out["members"][0]["email"] == "mike@work.com"


def test_merge_identity_is_idempotent_and_case_insensitive():
    team = {"members": [{"name": "Kim", "email": "kim@work.com", "role": "self",
                         "aliases": ["kim@home.com"], "accounts": ["u_me"]}]}
    out = connect.merge_identity_into_team(team, {"slack_user_id": "U_ME", "email": "KIM@HOME.com"})
    assert _self(out)["accounts"] == ["u_me"] and _self(out)["aliases"] == ["kim@home.com"]
    out = connect.merge_identity_into_team(out, {"github_login": "kim-gh", "email": "kim@other.com"})
    assert _self(out)["accounts"] == ["u_me", "kim-gh"]
    assert _self(out)["aliases"] == ["kim@home.com", "kim@other.com"]
    assert connect.merge_identity_into_team(out, {"github_login": "kim-gh", "email": "kim@other.com"}) == out


# ── connect ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_creates_source_identity_and_rules_idempotently(db, files):
    await connect.connect("slack")
    await connect.connect("slack")
    await connect.connect("github")

    rows = await db.execute_fetchall("SELECT * FROM sources WHERE source_type = 'claude_code' ORDER BY source_id")
    assert [(r["source_id"], r["workflow_id"], r["name"], r["space_id"], r["platform"]) for r in rows] == [
        ("src_claude_code_github", "claude_code:github", "GitHub (Claude Code)", "default", "github"),
        ("src_claude_code_slack", "claude_code:slack", "Slack (Claude Code)", "default", "slack"),
    ]
    source = await store.get_source("slack")
    assert source["tools"] == SLACK_TOOLS and source["identity"]["slack_user_id"] == "U_ME"
    assert source["config"] == store.default_config("slack") and source["enabled"]

    me = _self(files.team)
    assert me["accounts"] == ["U_ME", "kim-gh"] and me["email"] == "kim@work.com"
    assert len(files.team["members"]) == 2

    names = [r["name"] for r in files.rules["rules"]]
    assert names == ["Ignore bot messages", connect.RULE_BOTS, connect.RULE_GITHUB]
    assert connect.RULE_LINEAR not in names


@pytest.mark.asyncio
async def test_connect_events_resolve_to_space(db, files):
    await db.execute("INSERT INTO spaces (space_id, name) VALUES ('work', 'Work')")
    await db.commit()
    await connect.connect("slack", "work")
    from laya.pipeline.space_resolution import resolve_space
    assert await resolve_space("evt_x", "claude_code:slack", "slack") == "work"
    # Reconnecting without a space keeps the chosen one.
    await connect.connect("slack")
    assert await resolve_space("evt_y", "claude_code:slack", "slack") == "work"
    with pytest.raises(connect.ConnectError, match="does not exist"):
        await connect.connect("slack", "nope")


@pytest.mark.asyncio
async def test_connect_replaces_auto_registered_source(db, files):
    await db.execute(
        "INSERT INTO sources (source_id, name, platform, workflow_id) VALUES ('src_auto', 'x', 'slack', 'claude_code:slack')"
    )
    await db.commit()
    await connect.connect("slack")
    rows = await db.execute_fetchall("SELECT source_id FROM sources WHERE workflow_id = 'claude_code:slack'")
    assert [r["source_id"] for r in rows] == ["src_claude_code_slack"]


@pytest.mark.asyncio
async def test_connect_unavailable_platform(db, files):
    with pytest.raises(connect.ConnectError, match="no Linear"):
        await connect.connect("linear")
    with pytest.raises(connect.ConnectError, match="no identity"):
        await connect.connect("notion")
    assert await store.get_source("linear") is None


@pytest.mark.asyncio
async def test_connect_hooks(db, files):
    seen = []

    async def hook(platform, pd):
        seen.append((platform, pd.tools.get("send")))

    with patch.object(connect, "CONNECT_HOOKS", [hook]):
        await connect.connect("slack")
    assert seen == [("slack", "mcp__s__send")]


@pytest.mark.asyncio
async def test_disconnect_removes_rows_and_unneeded_rules(db, files):
    await connect.connect("slack")
    await connect.connect("github")
    await store.set_state("github", "x", "1")

    await connect.disconnect("github")
    assert await store.get_source("github") is None
    assert await store.get_state("github") == {}
    assert not await db.execute_fetchall("SELECT 1 FROM sources WHERE source_id = 'src_claude_code_github'")
    names = [r["name"] for r in files.rules["rules"]]
    assert connect.RULE_GITHUB not in names and connect.RULE_BOTS in names

    await connect.disconnect("slack")
    assert [r["name"] for r in files.rules["rules"]] == ["Ignore bot messages"]


@pytest.mark.asyncio
async def test_configure_validates(db, files):
    await connect.connect("slack")
    cfg = await connect.configure("slack", {"interval_minutes": 30, "active_hours": {"start": "07:00"},
                                            "context_messages": 4})
    assert cfg["interval_minutes"] == 30 and cfg["active_hours"]["end"] == "19:00"
    assert (await store.get_source("slack"))["config"]["context_messages"] == 4
    for bad in ({"nope": 1}, {"interval_minutes": "x"}, {"active_hours": {"days": [9]}},
                {"active_hours": {"start": "7"}}, {"timezone": "Mars/Base"}, {"orgs": []}):
        with pytest.raises(connect.ConnectError):
            await connect.configure("slack", bad)
    with pytest.raises(connect.ConnectError):
        await connect.configure("gmail", {})


# ── service ──────────────────────────────────────────────────────────────


def _src(cursor=None, **cfg):
    return {"platform": "slack", "cursor": cursor, "enabled": True,
            "config": {**store.default_config("slack"), "timezone": "UTC", **cfg}}


def test_due_from_cursor():
    now = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)
    assert service.is_due(_src(), now)
    assert not service.is_due(_src((now - timedelta(minutes=30)).isoformat()), now)
    assert service.is_due(_src((now - timedelta(minutes=61)).isoformat()), now)
    # The minimum interval guards against a zero setting.
    assert not service.is_due(_src((now - timedelta(minutes=2)).isoformat(), interval_minutes=0), now)


def test_active_hours():
    cfg = _src()["config"]
    monday = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)
    assert service.within_active_hours(cfg, monday)
    assert not service.within_active_hours(cfg, monday.replace(hour=20))
    assert not service.within_active_hours(cfg, monday - timedelta(days=1))  # Sunday
    berlin = {**cfg, "timezone": "Europe/Berlin"}
    assert not service.within_active_hours(berlin, monday.replace(hour=18, minute=30))


@pytest.mark.asyncio
async def test_sweep_posts_events_and_moves_cursor(db, files):
    from laya.claude_code.platforms.common import make_event

    await connect.connect("slack")
    ev = make_event(platform="slack", native_id="C1:1.0", timestamp=datetime.now(timezone.utc),
                    raw_event_type="message_received", actor_name="Mike", actor_email="",
                    actor_handle="U_MIKE", subject_type="thread", subject_id="C1:1.0",
                    subject_title="#general", subject_url=None, body="hi", metadata={})
    collector = AsyncMock(return_value=[ev])
    with patch.dict(service.COLLECTORS, {"slack": collector}), \
         patch.object(recheck, "recheck", new_callable=AsyncMock, return_value=[]):
        result = await service.sweep("slack")
        again = await service.sweep("slack")  # same id: dropped by POST /events
    assert result == {"events": 1, "error": None} and again["error"] is None
    rows = await db.execute_fetchall("SELECT event_id, space_id, processing_status FROM events")
    assert len(rows) == 1 and rows[0]["processing_status"] == "queued"
    source_arg, since = collector.await_args_list[0].args
    assert source_arg["deny_tools"] == list(SLACK_TOOLS.values())
    assert datetime.now(timezone.utc) - since > timedelta(hours=11)  # initial look-back
    _, since2 = collector.await_args_list[1].args
    assert datetime.now(timezone.utc) - since2 < timedelta(minutes=6)  # cursor minus overlap
    source = await store.get_source("slack")
    assert source["last_count"] == 1 and source["cursor"] and source["last_error"] is None


@pytest.mark.asyncio
async def test_sweep_failure_keeps_cursor(db, files):
    await connect.connect("slack")
    with patch.dict(service.COLLECTORS, {"slack": AsyncMock(side_effect=RuntimeError("MCP down"))}):
        result = await service.sweep("slack")
    assert result == {"events": 0, "error": "MCP down"}
    source = await store.get_source("slack")
    assert source["cursor"] is None and source["last_error"] == "MCP down" and source["last_run_at"]


@pytest.mark.asyncio
async def test_sweep_blocked_by_budget_and_space_pause(db, files):
    await connect.connect("slack")
    collector = AsyncMock(return_value=[])
    with patch.dict(service.COLLECTORS, {"slack": collector}):
        with patch("laya.pipeline.agent_budget.is_agent_budget_paused", new_callable=AsyncMock, return_value=True):
            assert (await service.sweep("slack"))["error"] == "agent budget paused"
        await db.execute("UPDATE spaces SET paused = 1 WHERE space_id = 'default'")
        await db.commit()
        assert (await service.sweep("slack"))["error"] == "space paused"
        assert await service.run_due_sweeps(datetime(2026, 2, 23, 10, tzinfo=timezone.utc)) == []
    collector.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_due_sweeps_is_restart_safe(db, files):
    await connect.connect("slack")
    await connect.configure("slack", {"timezone": "UTC"})
    now = datetime(2026, 2, 23, 10, tzinfo=timezone.utc)  # a Monday, inside active hours
    with patch.object(service, "sweep", new_callable=AsyncMock) as sweep:
        assert await service.run_due_sweeps(now) == ["slack"]
        # A recent cursor in the database (as after a restart) means not due.
        await store.upsert_source("slack", cursor=(now - timedelta(minutes=10)).isoformat())
        assert await service.run_due_sweeps(now) == []
        await store.upsert_source("slack", cursor=(now - timedelta(hours=2)).isoformat())
        assert await service.run_due_sweeps(now) == ["slack"]
        assert await service.run_due_sweeps(now.replace(hour=22)) == []
        await store.upsert_source("slack", enabled=False)
        assert await service.run_due_sweeps(now) == []
    assert sweep.await_count == 2


# ── recheck ──────────────────────────────────────────────────────────────


async def _open_card(db, *, platform="github", subject_type="pull_request", subject_id="o/r#7",
                     hours_ago=10, event_id="evt_ask", card_id="card_ask", metadata="{}"):
    ts = db_ts(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    await db.execute(
        """INSERT INTO events (event_id, timestamp, source_platform, source_connection_id,
               source_raw_event_type, subject_type, subject_id, subject_title, subject_url,
               content_metadata, raw_json, processing_status)
           VALUES (?, ?, ?, ?, 'comment', ?, ?, 't', 'https://x', ?, '{}', 'completed')""",
        (event_id, ts, platform, f"claude_code:{platform}", subject_type, subject_id, metadata),
    )
    await db.execute(
        """INSERT INTO action_cards (card_id, event_id, priority, persona, category, header,
               summary, status, entity_id, created_at)
           VALUES (?, ?, 'HIGH', 'ENGINEER', 'CODE', 'h', 's', 'ready', ?, ?)""",
        (card_id, event_id, f"{platform}:{subject_type}:{subject_id}", ts),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_recheck_due_entities(db):
    await _open_card(db)
    await _open_card(db, subject_id="o/r#8", event_id="evt_new", card_id="card_new", hours_ago=1)
    await _open_card(db, subject_id="o/r#9", event_id="evt_9", card_id="card_9")
    # A newer event in entity #9 means the card is not stale.
    await db.execute(
        """INSERT INTO events (event_id, timestamp, source_platform, source_connection_id,
               source_raw_event_type, subject_type, subject_id, raw_json)
           VALUES ('evt_9b', ?, 'github', 'claude_code:github', 'comment', 'pull_request', 'o/r#9', '{}')""",
        (db_ts(datetime.now(timezone.utc)),),
    )
    await db.commit()
    due = await recheck.due_entities("github", 6)
    assert [d["entity_id"] for d in due] == ["github:pull_request:o/r#7"]


@pytest.mark.asyncio
async def test_recheck_github_merged_and_commented(db):
    await _open_card(db)
    await _open_card(db, subject_type="ticket", subject_id="o/r#10", event_id="evt_10", card_id="card_10")
    await _open_card(db, subject_id="o/r#11", event_id="evt_11", card_id="card_11")
    later = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def gh(*args, **kw):
        path = args[1]
        if path == "repos/o/r/pulls/7":
            return {"state": "closed", "merged_at": later, "html_url": "https://github.com/o/r/pull/7"}
        if path == "repos/o/r/issues/10":
            return {"state": "open"}
        if path.startswith("repos/o/r/issues/10/comments"):
            return [{"user": {"login": "Kim-GH"}, "created_at": later, "html_url": "c10"}]
        if path == "repos/o/r/pulls/11":
            return {"state": "open"}
        return []

    source = {"config": store.default_config("github"), "identity": {"github_login": "kim-gh"}}
    with patch.object(recheck, "run_gh", side_effect=gh):
        events = await recheck.recheck("github", source)
        again = await recheck.recheck("github", source)
    assert again == []  # checked within recheck_after_hours
    by_subject = {e.subject.id: e for e in events}
    assert set(by_subject) == {"o/r#7", "o/r#10"}
    merged = by_subject["o/r#7"]
    assert merged.content.metadata["resolved_in_source"] is True
    assert merged.entity_id == "github:pull_request:o/r#7"
    assert merged.subject.url == "https://github.com/o/r/pull/7"
    assert by_subject["o/r#10"].actor.platform_handle == "kim-gh"
    assert by_subject["o/r#10"].entity_id == "github:ticket:o/r#10"


@pytest.mark.asyncio
async def test_recheck_slack_answered(db):
    await _open_card(db, platform="slack", subject_type="thread", subject_id="C1:100.0",
                     metadata='{"slack_channel_id": "C1", "slack_channel_type": "public"}')
    await _open_card(db, platform="slack", subject_type="thread", subject_id="D1",
                     event_id="evt_dm", card_id="card_dm",
                     metadata='{"slack_channel_id": "D1", "slack_channel_type": "dm"}')
    answer_at = str((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    out = {"items": [
        {"key": "slack:thread:C1:100.0", "answered": True, "at": answer_at, "url": "https://s/p1"},
        {"key": "slack:thread:D1", "answered": False},
        {"key": "unknown", "answered": True},
    ]}
    source = {"config": store.default_config("slack"), "identity": {"slack_user_id": "U_ME"},
              "tools": SLACK_TOOLS}
    with patch.object(recheck, "run_turn", new_callable=AsyncMock, return_value=out) as turn:
        events = await recheck.recheck("slack", source)
    prompt = turn.await_args.args[2]
    assert "mcp__s__thread channel_id=C1 thread_ts=100.0" in prompt
    assert "mcp__s__channel channel_id=D1" in prompt
    assert len(events) == 1
    e = events[0]
    assert e.actor.platform_handle == "U_ME" and e.subject.id == "C1:100.0"
    assert e.content.metadata["resolved_in_source"] is True
    assert e.content.metadata["slack_channel_id"] == "C1"


@pytest.mark.asyncio
async def test_recheck_linear_resolved(db):
    await _open_card(db, platform="linear", subject_type="ticket", subject_id="TRAD-1")
    out = {"items": [{"key": "linear:ticket:TRAD-1", "answered": False, "resolved": False, "status": "Done"}]}
    source = {"config": store.default_config("linear"), "identity": {"email": "kim@work.com"},
              "tools": {"get_issue": "li"}}
    with patch.object(recheck, "run_turn", new_callable=AsyncMock, return_value=out), \
         patch("laya.config.get_self_user", return_value=None):
        events = await recheck.recheck("linear", source)
    assert [e.subject.id for e in events] == ["TRAD-1"]
    assert recheck.recheck.__name__ and await recheck.recheck("notion", source) == []


# ── API ──────────────────────────────────────────────────────────────────


async def _client():
    from laya.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_api_status_connect_config_sync_disconnect(db, files):
    async with await _client() as client:
        resp = await client.get("/claude-code/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cli"]["found"] is True and data["budget_paused"] is False
        assert data["platforms"]["slack"]["available"] and not data["platforms"]["slack"]["connected"]
        assert data["platforms"]["slack"]["can_send"] is True
        assert data["platforms"]["github"]["can_send"] is False
        assert data["platforms"]["linear"]["problem"] == "no Linear"

        resp = await client.post("/claude-code/slack/connect", json={})
        assert resp.status_code == 200
        assert resp.json()["connected"] and resp.json()["identity"]["slack_user_id"] == "U_ME"
        assert (await client.post("/claude-code/linear/connect")).status_code == 422
        assert (await client.post("/claude-code/jira/connect")).status_code == 404

        resp = await client.put("/claude-code/slack/config", json={"interval_minutes": 15})
        assert resp.status_code == 200 and resp.json()["config"]["interval_minutes"] == 15
        assert (await client.put("/claude-code/slack/config", json={"bogus": 1})).status_code == 422

        with patch.object(service, "sweep", new_callable=AsyncMock, return_value={"events": 3, "error": None}):
            resp = await client.post("/claude-code/slack/sync")
        assert resp.json() == {"events": 3, "error": None}
        assert (await client.post("/claude-code/gmail/sync")).status_code == 404

        assert (await client.delete("/claude-code/slack")).status_code == 200
        data = (await client.get("/claude-code/status")).json()
        assert not data["platforms"]["slack"]["connected"]


@pytest.mark.asyncio
async def test_api_discover_forces_a_new_run(db, files):
    with patch.object(discover, "discover", new_callable=AsyncMock, return_value=_discovery()) as run:
        async with await _client() as client:
            resp = await client.post("/claude-code/discover")
    assert resp.status_code == 200
    assert run.await_args.kwargs == {"force": True, "with_identity": True}


@pytest.mark.asyncio
async def test_api_discover_refreshes_stored_tool_names(db, files):
    await connect.connect("slack")
    await connect.connect("gmail")
    renamed = {k: v.replace("mcp__s__", "mcp__claude_ai_Slack__") for k, v in SLACK_TOOLS.items()}
    fresh = _discovery(
        slack=PlatformDiscovery(available=True, tools=renamed, identity={"slack_user_id": "U_ME"}),
        # An unavailable platform keeps the names it had.
        gmail=PlatformDiscovery(available=False, problem="needs auth"),
    )
    with patch.object(discover, "discover", new_callable=AsyncMock, return_value=fresh):
        async with await _client() as client:
            resp = await client.post("/claude-code/discover")
    assert resp.status_code == 200
    assert (await store.get_source("slack"))["tools"] == renamed
    assert (await store.get_source("gmail"))["tools"] == {"search": "g1", "get_thread": "g2"}
    assert await discover.refresh_stored_tools(fresh) == []


@pytest.mark.asyncio
async def test_sweep_after_restart_uses_refreshed_tool_names(db, files):
    await connect.connect("slack")
    renamed = {**SLACK_TOOLS, "search": "mcp__new__search"}
    fresh = _discovery(slack=PlatformDiscovery(available=True, tools=renamed, identity={"slack_user_id": "U_ME"}))
    discover.reset_cache()
    seen = {}

    async def collector(source, since):
        seen.update(source)
        return []

    with patch.object(discover, "discover", new_callable=AsyncMock, return_value=fresh), \
         patch.dict(service.COLLECTORS, {"slack": collector}), \
         patch("laya.claude_code.recheck.recheck", new_callable=AsyncMock, return_value=[]):
        await service.sweep("slack")
    assert seen["tools"]["search"] == "mcp__new__search"
    assert seen["deny_tools"] == fresh.all_tools
