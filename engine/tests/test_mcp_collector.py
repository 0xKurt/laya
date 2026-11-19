# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MCP collector: normalization, self flags, dedupe ids, sweep cursors."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from laya.collectors import mcp_collector as mc

ME = "U0ME"


def _slack(**kw):
    base = {
        "channel_id": "C1", "channel_name": "eng", "channel_type": "public",
        "ts": "1795000000.000100", "thread_ts": None, "user_id": "U0OTHER",
        "user_name": "Alice", "text": "hi", "permalink": "https://x.slack.com/p1",
    }
    return {**base, **kw}


def test_slack_thread_reply_keys_by_thread():
    [e] = mc._slack_events([_slack(thread_ts="1794999999.000001")], ME)
    assert e.subject.id == "C1:1794999999.000001"
    assert e.source.raw_event_type == "thread_reply"
    assert e.content.metadata["is_self"] is False


def test_slack_dm_without_thread_keys_by_channel():
    [e] = mc._slack_events([_slack(channel_id="D1", channel_type="dm")], ME)
    assert e.subject.id == "D1"
    assert e.content.metadata["slack_channel_type"] == "dm"
    assert e.subject.title == "DM with Alice"


def test_slack_own_dm_message_is_titled_after_the_partner():
    own, theirs = mc._slack_events(
        [
            _slack(channel_id="D1", channel_type="dm", user_id=ME, user_name="Kurt"),
            _slack(channel_id="D1", channel_type="dm", ts="1795000001.0"),
        ],
        ME,
    )
    assert own.subject.title == theirs.subject.title == "DM with Alice"


def test_slack_channel_root_message_is_its_own_conversation():
    [e] = mc._slack_events([_slack()], ME)
    assert e.subject.id == "C1:1795000000.000100"


def test_slack_self_and_mention_flags():
    [own, mention] = mc._slack_events(
        [_slack(user_id=ME), _slack(ts="1795000001.0", text=f"<@{ME}> can you check?")], ME
    )
    assert own.content.metadata["is_self"] is True
    assert mention.content.metadata["slack_is_mention"] is True


def test_event_ids_are_stable_and_platform_scoped():
    a = mc._slack_events([_slack()], ME)[0].event_id
    b = mc._slack_events([_slack(text="edited")], ME)[0].event_id
    assert a == b
    assert a != mc._event_id("gmail", "C1:1795000000.000100")


_NOTES = {"message_id": "m1", "thread_id": "t1", "date": "2026-11-18T15:49:57Z",
          "from_email": "gemini-notes@google.com", "subject": "Notes: Sync",
          "url": "https://docs/1", "kind": "meeting_notes"}


def test_meeting_notes_become_one_card_per_action_item():
    body = (
        "SomniaLend Migration Sync – Nov 18, 2026\n"
        "Action items for Kurt Merbeth:\n"
        "- Remove Dropdown: Remove the dropdown and keep the save button.\n"
        "- Update Domain: Point the domain at the new deployment.\n"
        "Decisions:\n"
        "- Docs move to the platform site.\n"
        "Waiting on others:\n"
        "- Woodsoft: Wallet Connect integration.\n"
    )
    events = mc._gmail_events([{**_NOTES, "body": body}], {"me@example.com"})
    assert [e.subject.title for e in events] == [
        "Remove Dropdown (SomniaLend Migration Sync – Nov 18, 2026)",
        "Update Domain (SomniaLend Migration Sync – Nov 18, 2026)",
    ]
    first = events[0]
    assert first.source.raw_event_type == "meeting_action_item"
    assert first.subject.url == "https://docs/1"
    assert "Docs move to the platform site." in first.content.body
    assert "Woodsoft: Wallet Connect integration." in first.content.body
    assert len({e.event_id for e in events}) == 2


def test_meeting_without_items_for_me_makes_no_card():
    body = "Network Weekly – Nov 18, 2026\nDecisions:\n- Move to Tokyo mainnet.\n"
    assert mc._gmail_events([{**_NOTES, "body": body}], set()) == []


def test_gmail_self_mail():
    [mine] = mc._gmail_events(
        [{"message_id": "m2", "thread_id": "t2", "date": "2026-11-18T16:00:00Z",
          "from_email": "Me@Example.com", "subject": "Re: plan",
          "body": "done", "url": "https://mail/2", "kind": "email"}],
        {"me@example.com"},
    )
    assert mine.content.metadata["is_self"] is True
    assert mine.content.metadata["gmail_thread_id"] == "t2"


def test_linear_events_key_by_issue():
    [e] = mc._linear_events(
        [{"id": "n1", "created_at": "2026-11-18T10:00:00Z", "kind": "comment",
          "issue_identifier": "TRAD-1", "issue_title": "Fix", "issue_url": "https://linear/1",
          "text": "please review", "actor_email": "bob@example.com"}],
        {"me@example.com"},
    )
    assert e.subject.id == "TRAD-1"
    assert e.subject.type == "ticket"
    assert e.source.raw_event_type == "issue_comment"
    assert e.content.metadata["is_self"] is False


def test_parse_ts_variants():
    assert mc._parse_ts("1795000000.5").tzinfo is not None
    assert mc._parse_ts("2026-11-18T10:00:00Z") == datetime(2026, 11, 18, 10, tzinfo=timezone.utc)
    assert mc._parse_ts("2026-11-18T10:00:00").tzinfo == timezone.utc


def test_html_url():
    assert mc._html_url("https://api.github.com/repos/o/r/pulls/5") == "https://github.com/o/r/pull/5"
    assert mc._html_url(None) is None


def test_allowlists_contain_no_write_tools():
    for tools in mc.ALLOWED_TOOLS.values():
        for tool in tools:
            name = tool.rsplit("__", 1)[-1]
            assert not any(w in name for w in (
                "send", "reply", "forward", "create", "save", "update", "delete",
                "trash", "label", "mark", "add", "share", "schedule", "merge", "submit",
            )), tool


def test_active_hours():
    settings = mc.get_collector_settings() | {
        "timezone": "Europe/Berlin",
        "active_hours": {"start": "08:00", "end": "19:00", "days": [0, 1, 2, 3, 4]},
    }
    # Thursday 2026-11-19 09:00 Berlin = 08:00 UTC
    assert mc._within_active_hours(settings, datetime(2026, 11, 19, 8, tzinfo=timezone.utc))
    assert not mc._within_active_hours(settings, datetime(2026, 11, 19, 19, tzinfo=timezone.utc))
    # Saturday
    assert not mc._within_active_hours(settings, datetime(2026, 11, 21, 9, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_sweep_stores_events_and_advances_cursor(db):
    event = mc._slack_events([_slack()], ME)[0]
    settings = mc.get_collector_settings() | {"sources": {"slack": True}}
    with patch.object(mc, "get_collector_settings", return_value=settings), \
         patch.object(mc, "_collect_source", AsyncMock(return_value=[event])), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", AsyncMock(return_value=False)):
        before = datetime.now(timezone.utc)
        status = await mc.run_sweep(["slack"])

    assert status["slack"]["ok"] is True and status["slack"]["events"] == 1
    rows = await db.execute_fetchall("SELECT event_id FROM events WHERE event_id = ?", (event.event_id,))
    assert len(rows) == 1
    cursor = await mc._get_cursor("slack", 24)
    assert cursor >= before - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_failing_source_keeps_cursor_and_does_not_stop_others(db):
    settings = mc.get_collector_settings() | {"sources": {"slack": True, "github": True}}
    gh_event = mc._make_event(
        platform="github", native_id="merged:o/r#1", timestamp=datetime.now(timezone.utc),
        raw_event_type="pr_merged", actor_name="me", actor_email="", actor_handle="me",
        subject_type="pull_request", subject_id="o/r#1", subject_title="o/r#1: x",
        subject_url="https://github.com/o/r/pull/1", body="Merged", metadata={"is_self": True},
    )

    async def fake(source, since, s):
        if source == "slack":
            raise RuntimeError("boom")
        return [gh_event]

    with patch.object(mc, "get_collector_settings", return_value=settings), \
         patch.object(mc, "_collect_source", side_effect=fake), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", AsyncMock(return_value=False)):
        status = await mc.run_sweep(["slack", "github"])

    assert status["slack"]["ok"] is False and "boom" in status["slack"]["error"]
    assert status["github"]["ok"] is True
    rows = await db.execute_fetchall(
        "SELECT value FROM metadata WHERE key = ?", (mc._cursor_key("slack"),)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_sweep_skips_when_budget_paused(db):
    settings = mc.get_collector_settings() | {"sources": {"slack": True}}
    collect = AsyncMock()
    with patch.object(mc, "get_collector_settings", return_value=settings), \
         patch.object(mc, "_collect_source", collect), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", AsyncMock(return_value=True)):
        status = await mc.run_sweep(["slack"])
    assert status["slack"]["ok"] is False
    collect.assert_not_called()


def test_slack_burst_in_a_dm_is_one_event():
    msgs = [_slack(channel_id="D1", channel_type="dm", ts=f"179500000{i}.0", text=f"m{i}",
                   permalink=f"https://x/p{i}") for i in range(3)]
    [e] = mc._slack_events(msgs, ME)
    assert e.content.body == "Alice: m0\nAlice: m1\nAlice: m2"
    assert e.subject.url == "https://x/p0"          # where the user picks up
    assert e.timestamp == mc._parse_ts("1795000002.0")
    assert e.content.metadata["slack_message_count"] == 3


def test_slack_conversation_ending_with_own_reply_is_only_a_self_event():
    msgs = [
        _slack(channel_id="D1", channel_type="dm", ts="1795000000.0"),
        _slack(channel_id="D1", channel_type="dm", ts="1795000001.0", user_id=ME),
    ]
    [e] = mc._slack_events(msgs, ME)
    assert e.content.metadata["is_self"] is True


def test_slack_messages_after_own_reply_stay_open():
    msgs = [
        _slack(channel_id="D1", channel_type="dm", ts="1795000000.0", text="q1"),
        _slack(channel_id="D1", channel_type="dm", ts="1795000001.0", user_id=ME, text="a1"),
        _slack(channel_id="D1", channel_type="dm", ts="1795000002.0", text="q2"),
    ]
    own, rest = mc._slack_events(msgs, ME)
    assert own.content.metadata["is_self"] and own.content.body == "a1"
    assert not rest.content.metadata["is_self"]
    # The reply carries the conversation so far, the user's own answer included.
    assert "Earlier in the conversation:" in rest.content.body
    assert "Alice: q1" in rest.content.body and "Me: a1" in rest.content.body
    assert rest.content.body.split("New:\n", 1)[1].endswith("Alice: q2")
    assert rest.timestamp > own.timestamp


def test_slack_channel_title_has_one_hash():
    [e] = mc._slack_events([_slack(channel_name="#eng")], ME)
    assert e.subject.title == "#eng"


def test_gmail_repeated_alerts_in_a_thread_are_one_event():
    msgs = [
        {"message_id": f"m{i}", "thread_id": "t1", "date": f"2026-11-19T09:0{i}:00Z",
         "from_name": "Rootly", "from_email": "on-call@rootly.com", "subject": "Alert",
         "body": f"fired {i}", "url": f"https://mail/{i}", "kind": "email"}
        for i in range(3)
    ]
    [e] = mc._gmail_events(msgs, {"me@example.com"})
    assert e.content.metadata["gmail_message_count"] == 3
    assert "fired 0" in e.content.body and "fired 2" in e.content.body
    assert e.event_id == mc._event_id("gmail", "m2")


def test_is_mine_keeps_dms_mentions_own_and_my_threads_only():
    mine = set()
    assert mc._is_mine(_slack(channel_type="dm"), ME, mine)
    assert mc._is_mine(_slack(channel_type="group_dm"), ME, mine)
    assert mc._is_mine(_slack(user_id=ME), ME, mine)
    assert mc._is_mine(_slack(text=f"hey <@{ME}>"), ME, mine)
    other_thread = _slack(thread_ts="1794999999.000001", text="unrelated chatter")
    assert not mc._is_mine(other_thread, ME, mine)
    assert mc._is_mine(other_thread, ME, {"C1:1794999999.000001"})
    assert not mc._is_mine(_slack(channel_name="general", text="link"), ME, mine)


def test_slack_prompt_reads_known_threads_and_no_broad_thread_search():
    prompt = mc._slack_prompt(ME, datetime(2026, 11, 19, tzinfo=timezone.utc), [("C1", "1794999999.000001")])
    assert "slack_read_thread" in prompt and "channel_id=C1 thread_ts=1794999999.000001" in prompt
    assert "is:thread" not in prompt
    assert "thread_ts=" not in mc._slack_prompt(ME, datetime(2026, 11, 19, tzinfo=timezone.utc), [])


@pytest.mark.asyncio
async def test_my_slack_threads_expire_after_14_days(db):
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    await mc._set_my_slack_threads({"C1:1": fresh, "C2:2": stale})
    assert await mc._get_my_slack_threads() == {"C1:1": fresh}


def test_linear_review_requests_and_issueless_items_are_dropped():
    base = {"id": "n", "created_at": "2026-11-19T10:00:00Z", "kind": "other",
            "issue_title": "x", "issue_url": "https://linear/x"}
    events = mc._linear_events([
        {**base, "id": "1", "issue_identifier": "", "text": "Dave requested a review"},
        {**base, "id": "2", "issue_identifier": "TRAD-1", "text": "Dave Johnston requested markets-team review of PR"},
        {**base, "id": "3", "issue_identifier": "TRAD-2", "kind": "comment", "text": "can you check the owner?"},
    ], set())
    assert [e.subject.id for e in events] == ["TRAD-2"]


def _notification(reason, *, type_="PullRequest", nid="1"):
    return {
        "id": nid, "reason": reason, "updated_at": "2026-11-19T10:00:00Z",
        "repository": {"full_name": "somnia-chain/x", "owner": {"login": "somnia-chain"}},
        "subject": {"type": type_, "title": "t", "url": f"https://api.github.com/repos/somnia-chain/x/pulls/{nid}",
                    "latest_comment_url": None},
    }


@pytest.mark.asyncio
async def test_github_keeps_own_prs_and_mentions_but_not_others_reviews():
    notes = [
        _notification("author", nid="1"),         # my PR → keep
        _notification("comment", nid="2"),        # I reviewed someone's PR → drop
        _notification("mention", nid="3"),        # @-mentioned on someone's PR → keep
        _notification("review_requested", nid="4"),
        _notification("comment", nid="5", type_="Issue"),  # issue thread → keep
    ]

    async def fake_gh(*args):
        if args[:2] == ("api", "--paginate"):
            return [notes]
        if args[0] == "api" and args[1].startswith("https://api.github.com/repos/somnia-chain/x/pulls/"):
            return {"user": {"login": "someone-else"}}
        return {"items": []}

    settings = mc.get_collector_settings() | {
        "identity": {"github_login": "me", "slack_user_id": "", "emails": []},
        "github_orgs": ["somnia-chain"],
    }
    with patch.object(mc, "_gh_json", side_effect=fake_gh):
        events = await mc._collect_github(datetime(2026, 11, 19, tzinfo=timezone.utc), settings)
    assert sorted(e.subject.id for e in events) == ["somnia-chain/x#1", "somnia-chain/x#3", "somnia-chain/x#5"]


def test_linear_done_issue_is_a_closing_event():
    base = {"created_at": "2026-11-19T10:00:00Z", "kind": "status_changed", "issue_title": "x",
            "issue_url": "https://linear/x", "text": "moved"}
    done, open_ = mc._linear_events([
        {**base, "id": "1", "issue_identifier": "L1-642", "issue_status": "Done"},
        {**base, "id": "2", "issue_identifier": "L1-643", "issue_status": "In Progress"},
    ], set())
    assert done.content.metadata["is_self"] and done.content.metadata["resolved_in_source"]
    assert not open_.content.metadata["is_self"] and not open_.content.metadata["resolved_in_source"]


def test_slack_context_messages_explain_a_reply_but_trigger_nothing():
    msgs = [
        _slack(channel_id="D1", channel_type="dm", ts="1795000000.0", user_id=ME, text="still at X?", is_context=True),
        _slack(channel_id="D1", channel_type="dm", ts="1795000001.0", text="yeah, not sure for how long", is_context=False),
    ]
    [e] = mc._slack_events(msgs, ME)               # no self event from a context message
    assert not e.content.metadata["is_self"]
    assert "Me: still at X?" in e.content.body
    assert e.content.body.split("New:\n", 1)[1].endswith("Alice: yeah, not sure for how long")


def test_slack_context_only_conversation_produces_nothing():
    assert mc._slack_events([_slack(channel_type="dm", channel_id="D1", is_context=True)], ME) == []


def test_normalize_slack_channel_types():
    n = lambda **kw: mc._normalize_slack(_slack(**kw))["channel_type"]
    assert n(channel_type="im") == "dm"
    assert n(channel_type="", channel_id="D123") == "dm"
    assert n(channel_type="mpim") == "group_dm"
    assert n(channel_type="private_channel") == "private"
    assert n(channel_type="channel") == "public"
    assert n(channel_type=None) == "public"


@pytest.mark.asyncio
async def test_loop_does_not_resweep_after_restart(db):
    # A cursor 10 minutes old means the source ran recently, even with no in-memory time.
    for source in mc.SOURCES:
        await mc._set_cursor(source, datetime.now(timezone.utc) - timedelta(minutes=10))
    mc._last_sweep.clear()
    settings = mc.get_collector_settings() | {"enabled": True, "interval_minutes": 60}
    sweep = AsyncMock()
    calls = {"n": 0}

    async def stop_after_one(_):
        calls["n"] += 1
        raise asyncio.CancelledError

    with patch.object(mc, "get_collector_settings", return_value=settings), \
         patch.object(mc, "_within_active_hours", return_value=True), \
         patch.object(mc, "run_sweep", sweep), \
         patch("asyncio.sleep", side_effect=stop_after_one):
        with pytest.raises(asyncio.CancelledError):
            await mc._collector_loop()
    sweep.assert_not_called()
