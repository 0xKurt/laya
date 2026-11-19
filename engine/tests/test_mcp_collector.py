# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MCP collector: normalization, self flags, dedupe ids, sweep cursors."""

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


def test_gmail_meeting_notes_and_self():
    events = mc._gmail_events(
        [
            {"message_id": "m1", "thread_id": "t1", "date": "2026-11-18T15:49:57Z",
             "from_email": "gemini-notes@google.com", "subject": "Notes: Sync",
             "body": "- Kurt: ship the fix", "url": "https://mail/1", "kind": "meeting_notes"},
            {"message_id": "m2", "thread_id": "t2", "date": "2026-11-18T16:00:00Z",
             "from_email": "Me@Example.com", "subject": "Re: plan",
             "body": "done", "url": "https://mail/2", "kind": "email"},
        ],
        {"me@example.com"},
    )
    notes, mine = events
    assert notes.subject.type == "meeting"
    assert notes.source.raw_event_type == "meeting_notes"
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
