# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""My work refreshes and row actions update the related board cards (claude_code/related.py)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from laya.claude_code import related, store
from laya.db.timeutil import db_ts
from laya.pipeline.card_status import RESOLVED_IN_SOURCE_REASON, apply_conclusion, decide_card_status

NOW = datetime.now(timezone.utc)
PR_URL = "https://github.com/o/r/pull/7"


async def _card(db, card_id, *, platform="github", subject_type="pull_request", subject_id="o/r#7",
                hours_ago=3.0, source_url=None, header="h", summary="s", status="ready", set_by=None,
                event_id=None, created_hours_ago=None):
    event_id = event_id or f"evt_{card_id}"
    ts = db_ts(NOW - timedelta(hours=hours_ago))
    created = db_ts(NOW - timedelta(hours=created_hours_ago if created_hours_ago is not None else hours_ago))
    await db.execute(
        """INSERT INTO events (event_id, timestamp, source_platform, source_connection_id,
               source_raw_event_type, subject_type, subject_id, subject_title, subject_url,
               content_metadata, raw_json, processing_status)
           VALUES (?, ?, ?, ?, 'comment', ?, ?, 't', ?, '{}', '{}', 'completed')""",
        (event_id, ts, platform, f"claude_code:{platform}", subject_type, subject_id, source_url),
    )
    await db.execute(
        """INSERT INTO action_cards (card_id, event_id, priority, persona, category, header,
               summary, status, status_set_by, entity_id, source_url, created_at)
           VALUES (?, ?, 'HIGH', 'ENGINEER', 'CODE', ?, ?, ?, ?, ?, ?, ?)""",
        (card_id, event_id, header, summary, status, set_by,
         f"{platform}:{subject_type}:{subject_id}", source_url, created),
    )
    await db.commit()


async def _status(db, card_id):
    rows = await db.execute_fetchall(
        "SELECT status, status_set_by, status_reason FROM action_cards WHERE card_id = ?", (card_id,))
    return dict(rows[0])


@pytest_asyncio.fixture
async def posted(db):
    """Post events like receive_event and apply the conclusion like the queue does."""
    events = []

    async def post(event):
        events.append(event)
        await db.execute(
            """INSERT OR IGNORE INTO events (event_id, timestamp, source_platform, source_connection_id,
                   source_raw_event_type, subject_type, subject_id, subject_title, subject_url,
                   content_metadata, raw_json, processing_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
            (event.event_id, db_ts(event.timestamp), event.source.platform, event.source.connection_id,
             event.source.raw_event_type, event.subject.type, event.subject.id, event.subject.title,
             event.subject.url, json.dumps(event.content.metadata), event.model_dump_json()),
        )
        await db.commit()
        await apply_conclusion(event)

    for platform in ("github", "linear", "slack"):
        await store.upsert_source(platform, enabled=True)
    with patch.object(related, "_post", side_effect=post), \
         patch("laya.models.card_lifecycle.manager.broadcast", new_callable=AsyncMock), \
         patch("laya.pipeline.summarize.trigger_summary_status_update", new_callable=AsyncMock):
        yield events


def _pr(number=7, **kw):
    return {"repo": "o/r", "number": number, "url": f"https://github.com/o/r/pull/{number}",
            "checks": {"state": "pending", "failing": 0}, "changes_requested": 0, "approvals": 0,
            "unresolved_threads": 0, "review_decision": None, "conflicts": False, "draft": False, **kw}


def _gh(states):
    async def gh(*args, **kw):
        path = args[1]
        for number, state in states.items():
            if path == f"repos/o/r/pulls/{number}":
                return state
        return {"state": "open"}
    return gh


def _merged(hours_ago=1.0, number=7):
    return {"state": "closed", "merged_at": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "html_url": f"https://github.com/o/r/pull/{number}"}


def test_refs_normalise_urls_and_identifiers():
    assert related.text_refs("see https://github.com/O/R/pull/7/files#diff-1") == {"gh:o/r#7"}
    assert related.text_refs("https://github.com/o/r/pulls/8") == {"gh:o/r#8"}
    assert related.text_refs("https://linear.app/acme/issue/TRAD-12/some-title") == {"lin:TRAD-12"}
    assert related.text_refs("TRAD-12 and ERC-20, branch kurt/trad-9") == {"lin:TRAD-12"}
    assert related.entity_ref("github:pull_request:o/r#7") == "gh:o/r#7"
    assert related.entity_ref("linear:ticket:trad-3") == "lin:TRAD-3"


def test_diff_classifies_changes():
    old = [_pr(7), _pr(8), _pr(9)]
    new = [_pr(8, checks={"state": "failure", "failing": 1}), _pr(9)]
    changes = {c.ref: c for c in related.diff_items("github", old, new)}
    assert changes["gh:o/r#7"].kind == "gone"
    assert changes["gh:o/r#8"].kind == "changed" and changes["gh:o/r#8"].reasons == ["checks failure"]
    assert "gh:o/r#9" not in changes
    done = related.diff_row("linear", "TRAD-1", {"identifier": "TRAD-1"}, None,
                            {"identifier": "TRAD-1", "state": "Canceled", "state_type": "canceled"})
    assert done.kind == "resolved" and done.note == "Linear status: Canceled."


@pytest.mark.asyncio
async def test_merged_pr_concludes_matching_cards(db, posted):
    await _card(db, "card_pr", source_url=PR_URL)
    # Another conversation that only refers to the PR (a sub-page URL).
    await _card(db, "card_slack", platform="slack", subject_type="thread", subject_id="C1:1.0",
                summary=f"Please review {PR_URL}/files")
    await _card(db, "card_other", subject_id="o/r#9", source_url="https://github.com/o/r/pull/9")
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: _merged()})):
        out = await related.after_refresh("github", [_pr(7), _pr(9)], [_pr(9)])
    reason = f"{RESOLVED_IN_SOURCE_REASON}: {PR_URL}"
    assert await _status(db, "card_pr") == {"status": "done", "status_set_by": "laya", "status_reason": reason}
    assert (await _status(db, "card_slack"))["status"] == "done"
    assert (await _status(db, "card_other"))["status"] == "ready"
    assert {c["entity_id"] for c in out["concluded"]} == {"github:pull_request:o/r#7", "slack:thread:C1:1.0"}
    assert all(e.content.metadata["resolved_in_source"] for e in posted)


@pytest.mark.asyncio
async def test_closed_unmerged_pr_concludes(db, posted):
    await _card(db, "card_pr", source_url=PR_URL)
    closed = {"state": "closed", "merged_at": None, "closed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "html_url": PR_URL}
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: closed})):
        await related.after_refresh("github", [_pr(7)], [])
    assert (await _status(db, "card_pr"))["status"] == "done"
    assert posted[0].content.body == "Closed on GitHub."


@pytest.mark.asyncio
async def test_pr_that_only_left_the_scope_stays(db, posted):
    await _card(db, "card_pr", source_url=PR_URL)
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({})):
        await related.after_refresh("github", [_pr(7)], [])
    assert (await _status(db, "card_pr"))["status"] == "ready"
    assert posted == []


@pytest.mark.asyncio
async def test_card_created_just_after_the_merge_is_concluded(db, posted):
    # The sweep read the PR 20 seconds after it was merged: the card is newer than the merge.
    await _card(db, "card_pr", source_url=PR_URL, hours_ago=1.0)
    merged = _merged(hours_ago=1.0 + 20 / 3600)
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: merged})):
        await related.after_refresh("github", [_pr(7)], [])
    assert (await _status(db, "card_pr"))["status"] == "done"


@pytest.mark.asyncio
async def test_missed_merge_found_for_open_card_not_in_list(db, posted):
    # No previous list (first refresh after a restart): an open card about a PR
    # that is not in the open list is checked with gh, once per cooldown.
    await _card(db, "card_pr", source_url=PR_URL)
    gh = AsyncMock(side_effect=_gh({7: _merged()}))
    with patch("laya.claude_code.recheck.run_gh", gh):
        await related.after_refresh("github", None, [_pr(9)])
        await related.after_refresh("github", None, [_pr(9)])
    assert (await _status(db, "card_pr"))["status"] == "done"
    assert gh.await_count == 1


@pytest.mark.asyncio
async def test_linear_done_and_canceled_conclude(db, posted):
    await _card(db, "card_lin", platform="linear", subject_type="ticket", subject_id="TRAD-1",
                source_url="https://linear.app/acme/issue/TRAD-1")
    await _card(db, "card_lin2", platform="linear", subject_type="ticket", subject_id="TRAD-2")
    await _card(db, "card_slack", platform="slack", subject_type="thread", subject_id="C1:2.0",
                header="Mike: can you finish TRAD-2?")
    done = {"identifier": "TRAD-1", "state": "Done", "state_type": "completed",
            "url": "https://linear.app/acme/issue/TRAD-1"}
    await related.after_row_action("linear", "TRAD-1", {"identifier": "TRAD-1"}, None, done)
    canceled = {"identifier": "TRAD-2", "state": "Canceled", "state_type": "canceled",
                "url": "https://linear.app/acme/issue/TRAD-2"}
    await related.after_row_action("linear", "TRAD-2", {"identifier": "TRAD-2"}, None, canceled)
    first = await _status(db, "card_lin")
    assert first["status"] == "done"
    assert first["status_reason"] == f"{RESOLVED_IN_SOURCE_REASON}: https://linear.app/acme/issue/TRAD-1"
    assert (await _status(db, "card_lin2"))["status"] == "done"
    assert (await _status(db, "card_slack"))["status"] == "done"


@pytest.mark.asyncio
async def test_no_matching_card_does_nothing(db, posted):
    await _card(db, "card_x", platform="slack", subject_type="thread", subject_id="C1:3.0", header="lunch?")
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: _merged()})):
        out = await related.after_refresh("github", [_pr(7)], [])
    assert posted == [] and out["concluded"] == []
    assert (await _status(db, "card_x"))["status"] == "ready"


@pytest.mark.asyncio
async def test_card_with_another_open_ask_stays(db, posted):
    # Same Slack thread: one card about the merged PR, one about another PR.
    await _card(db, "card_a", platform="slack", subject_type="thread", subject_id="C1:4.0",
                event_id="evt_a", summary=f"review {PR_URL}")
    await _card(db, "card_b", platform="slack", subject_type="thread", subject_id="C1:4.0",
                event_id="evt_b", summary="also https://github.com/o/r/pull/9 please")
    # A card naming the merged PR and an open Linear issue.
    await _card(db, "card_c", platform="slack", subject_type="thread", subject_id="C1:5.0",
                summary=f"{PR_URL} and TRAD-77")
    # A card the user reopened.
    await _card(db, "card_user", subject_id="o/r#7", source_url=PR_URL, set_by="user")
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: _merged()})):
        await related.after_refresh("github", [_pr(7), _pr(9)], [_pr(9)])
    for card in ("card_a", "card_b", "card_c", "card_user"):
        assert (await _status(db, card))["status"] == "ready", card


@pytest.mark.asyncio
async def test_ask_after_the_merge_stays(db, posted):
    await _card(db, "card_late_ask", platform="slack", subject_type="thread", subject_id="C1:6.0",
                summary=f"{PR_URL} is merged, please deploy it", hours_ago=0.5)
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: _merged(hours_ago=1.0)})):
        await related.after_refresh("github", [_pr(7)], [])
    assert (await _status(db, "card_late_ask"))["status"] == "ready"


@pytest.mark.asyncio
async def test_late_card_after_conclusion_is_created_done(db, posted):
    await _card(db, "card_pr", source_url=PR_URL)
    with patch("laya.claude_code.recheck.run_gh", side_effect=_gh({7: _merged()})):
        await related.after_refresh("github", [_pr(7)], [])
    # An earlier event of the PR finishes processing only now.
    decision = await decide_card_status(
        db, entity_id="github:pull_request:o/r#7", event_id="evt_late",
        event_ts=db_ts(NOW - timedelta(hours=5)), base_status="ready", verdict=None)
    assert decision.status == "done"
    assert decision.status_reason == f"{RESOLVED_IN_SOURCE_REASON}: {PR_URL}"


@pytest.mark.asyncio
async def test_non_concluding_change_rechecks_now(db, posted):
    await _card(db, "card_pr", source_url=PR_URL, hours_ago=1.0)  # younger than recheck_after_hours
    check = AsyncMock(return_value=[])
    with patch("laya.claude_code.recheck.check_entities", check), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", new_callable=AsyncMock, return_value=False):
        out = await related.after_refresh(
            "github", [_pr(7)], [_pr(7, checks={"state": "failure", "failing": 2}, changes_requested=1)])
    platform, _, entities = check.await_args.args
    assert platform == "github" and [e["entity_id"] for e in entities] == ["github:pull_request:o/r#7"]
    assert out["rechecked"] == [{"platform": "github", "checked": 1, "resolved": 0}]
    assert (await _status(db, "card_pr"))["status"] == "ready"


@pytest.mark.asyncio
async def test_recheck_respects_budget_limit_and_logins(db, posted):
    for i in range(5):
        await _card(db, f"card_s{i}", platform="slack", subject_type="thread", subject_id=f"C1:{i}.5",
                    header=f"Mike: what about TRAD-{i + 1}?")
    old = [{"identifier": f"TRAD-{i + 1}", "state": "Todo", "priority": 2} for i in range(5)]
    new = [{**o, "priority": 1} for o in old]
    check = AsyncMock(return_value=[])
    paused = AsyncMock(return_value=True)
    with patch("laya.claude_code.recheck.check_entities", check), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", paused):
        await related.after_refresh("linear", old, new)
    assert check.await_count == 0  # agent budget paused: no model turn

    paused.return_value = False
    with patch("laya.claude_code.recheck.check_entities", check), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", paused), \
         patch("laya.claude_code.service._assign_account", new_callable=AsyncMock, return_value=None):
        await related.after_refresh("linear", old, new)
    assert check.await_count == 0  # no eligible login

    with patch("laya.claude_code.recheck.check_entities", check), \
         patch("laya.pipeline.agent_budget.is_agent_budget_paused", paused), \
         patch("laya.claude_code.service._assign_account", new_callable=AsyncMock,
               return_value={"account_id": "a1", "tools": {}}):
        await related.after_refresh("linear", old, new)
    assert check.await_count == 1
    platform, source, entities = check.await_args.args
    assert platform == "slack" and len(entities) == related.MCP_RECHECK_LIMIT
    assert source["account_id"] == "a1"
