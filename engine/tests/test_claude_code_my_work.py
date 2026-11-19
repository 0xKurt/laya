# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

""""My work" section of the Claude Code connector (open Linear issues, own PRs)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from laya.claude_code import cli, discover, my_work, store
from laya.claude_code.discover import Discovery


@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    """No test of the connector may spawn `claude` or `gh`."""

    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(cli, "_run_process", side_effect=_refuse), \
         patch.object(cli, "read_init", side_effect=_refuse), \
         patch.object(cli, "cli_version", side_effect=_refuse), \
         patch.object(cli, "run_gh", side_effect=_refuse):
        discover._cache = Discovery(all_tools=[
            "mcp__linear-server__list_issues", "mcp__linear-server__save_issue",
        ])
        yield
        discover.reset_cache()


@pytest.fixture
def budget_open():
    with patch("laya.pipeline.agent_budget.is_agent_budget_paused", new_callable=AsyncMock,
               return_value=False):
        yield


def _node(number, **kw):
    node = {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/acme/app/pull/{number}",
        "isDraft": False, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-02T00:00:00Z",
        "baseRefName": "main", "headRefName": f"kurt/trad-{number}-fix", "mergeable": "MERGEABLE",
        "reviewDecision": "REVIEW_REQUIRED", "repository": {"nameWithOwner": "acme/app"},
        "reviewRequests": {"totalCount": 2},
        "latestReviews": {"nodes": [{"state": "COMMENTED", "author": {"login": "coderabbitai"}}]},
        "reviewThreads": {"nodes": [{"isResolved": False}, {"isResolved": True}]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "StatusContext", "state": "ERROR"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]}}}}]},
    }
    node.update(kw)
    return node


def _page(nodes, cursor=None):
    return {"data": {"search": {"issueCount": len(nodes), "nodes": nodes,
                                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}}


def _issue(i, st="started", **kw):
    return {"i": i, "t": f"Issue {i}", "u": f"https://linear.app/acme/issue/{i}", "s": st.title(),
            "st": st, "p": 2, "pr": "Proj", "d": None, "up": "2026-11-18T10:00:00Z", "l": [], **kw}


# ── parsing ──────────────────────────────────────────────────────────────


def test_search_query_is_authored_prs_only():
    assert my_work.search_query([]) == "is:pr is:open author:@me archived:false"
    q = my_work.search_query(["acme", "", "other"])
    assert q.endswith("org:acme org:other")
    assert "review-requested" not in q and "involves" not in q and "mentions" not in q


def test_linear_key_from_title_then_branch():
    assert my_work.linear_key("TRAD-12: fix", "kurt/smk-3") == "TRAD-12"
    assert my_work.linear_key("bump ERC-20 helper", "kurt/trad-571-lend") == "TRAD-571"
    assert my_work.linear_key("UTF-8 fix", "main") is None


def test_parse_pr_maps_review_ci_and_conflicts():
    pr = my_work.parse_pr(_node(7, mergeable="CONFLICTING", isDraft=True))
    assert pr["repo"] == "acme/app" and pr["number"] == 7 and pr["draft"]
    assert pr["conflicts"] and pr["review_decision"] == "REVIEW_REQUIRED"
    assert pr["requested_reviewers"] == 2 and pr["unresolved_threads"] == 1
    assert pr["checks"] == {"state": "failure", "failing": 2, "pending": 1, "total": 4}
    assert pr["linear_key"] == "TRAD-7"
    assert my_work.parse_pr({}) is None
    no_ci = my_work.parse_pr(_node(8, commits={"nodes": [{"commit": {"statusCheckRollup": None}}]}))
    assert no_ci["checks"]["state"] == "none"


@pytest.mark.asyncio
async def test_fetch_github_paginates_with_orgs():
    calls = []

    async def gh(*args, **kw):
        calls.append(args)
        return _page([_node(1)], cursor="c1") if len(calls) == 1 else _page([_node(2)])

    with patch.object(cli, "run_gh", side_effect=gh):
        prs = await my_work.fetch_github({"orgs": ["acme"]})
    assert [p["number"] for p in prs] == [1, 2]
    assert "q=is:pr is:open author:@me archived:false org:acme" in calls[0]
    assert "after=c1" in calls[1]


def test_merge_issues_flags_and_dedupes():
    rows = my_work.merge_issues({
        "assigned": [_issue("T-1"), _issue("T-2", st="backlog")],
        "created": [_issue("T-1"), _issue("T-3", st="unstarted", a="Byron"), _issue("T-4", st="completed")],
    })
    by = {r["identifier"]: r for r in rows}
    assert set(by) == {"T-1", "T-2", "T-3"}
    assert by["T-1"]["assigned_to_me"] and by["T-1"]["created_by_me"]
    assert by["T-2"]["assigned_to_me"] and not by["T-2"]["created_by_me"]
    assert by["T-3"]["created_by_me"] and not by["T-3"]["assigned_to_me"]
    assert by["T-3"]["assignee"] == "Byron" and by["T-3"]["state_type"] == "unstarted"


def test_linear_prompt_asks_for_both_lists_in_open_states():
    p = my_work.linear_prompt("mcp__linear-server__list_issues")
    assert 'assignee="me"' in p and 'creator="me"' in p
    for st in ("started", "unstarted", "backlog", "triage"):
        assert f'"{st}"' in p
    assert "completed" not in p


# ── entry point ──────────────────────────────────────────────────────────


async def _connect(platform, **fields):
    await store.upsert_source(platform, **fields)


@pytest.mark.asyncio
async def test_not_connected_returns_empty_sections(db):
    out = await my_work.get_my_work()
    assert out["github"] == {"prs": [], "connected": False, "error": None, "note": None,
                             "fetched_at": None, "cached": False}
    assert out["linear"]["issues"] == [] and not out["linear"]["connected"]


@pytest.mark.asyncio
async def test_sections_fetch_and_cache(db, budget_open):
    await _connect("github", config={"orgs": []})
    await _connect("linear", tools={"list_issues": "mcp__linear-server__list_issues"})
    collect = AsyncMock(return_value={"assigned": [_issue("T-1")], "created": []})
    gh = AsyncMock(return_value=_page([_node(1)]))
    with patch.object(cli, "run_gh", gh), patch.object(cli, "run_collect", collect):
        first = await my_work.get_my_work()
        second = await my_work.get_my_work()
        assert gh.await_count == 1 and collect.await_count == 1
        assert second["github"]["cached"] and second["linear"]["issues"][0]["identifier"] == "T-1"
        await my_work.get_my_work(refresh=True)
        assert gh.await_count == 2 and collect.await_count == 2

    assert first["github"]["prs"][0]["number"] == 1 and first["github"]["fetched_at"]
    args, kwargs = collect.await_args
    assert args[3] == ["mcp__linear-server__list_issues"]
    assert "mcp__linear-server__save_issue" in kwargs["deny"]


@pytest.mark.asyncio
async def test_cache_expires(db, budget_open):
    await _connect("github")
    old = (datetime.now(timezone.utc) - timedelta(seconds=my_work.CACHE_SECONDS + 5)).isoformat()
    await store.set_state(my_work.STATE_PLATFORM, "github", json.dumps({"items": [], "fetched_at": old}))
    gh = AsyncMock(return_value=_page([_node(3)]))
    with patch.object(cli, "run_gh", gh):
        out = await my_work.get_my_work()
    assert gh.await_count == 1 and out["github"]["prs"][0]["number"] == 3


@pytest.mark.asyncio
async def test_budget_pause_skips_linear_turn_and_returns_cache(db):
    await _connect("linear", tools={"list_issues": "mcp__linear-server__list_issues"})
    cached = {"items": [{"identifier": "T-9"}], "fetched_at": "2026-01-01T00:00:00+00:00"}
    await store.set_state(my_work.STATE_PLATFORM, "linear", json.dumps(cached))
    collect = AsyncMock()
    with patch("laya.pipeline.agent_budget.is_agent_budget_paused", new_callable=AsyncMock,
               return_value=True), patch.object(cli, "run_collect", collect):
        out = await my_work.get_my_work(refresh=True)
    collect.assert_not_awaited()
    assert out["linear"]["issues"] == [{"identifier": "T-9"}]
    assert "paused" in out["linear"]["note"] and out["linear"]["error"] is None


@pytest.mark.asyncio
async def test_errors_are_reported_per_section(db, budget_open):
    await _connect("github")
    await _connect("linear", tools={})
    with patch.object(cli, "run_gh", AsyncMock(side_effect=cli.ClaudeCodeError("gh api graphql failed"))):
        out = await my_work.get_my_work()
    assert out["github"]["error"] == "gh api graphql failed" and out["github"]["prs"] == []
    assert "list-issues tool" in out["linear"]["error"]


@pytest.mark.asyncio
async def test_endpoint(db, budget_open):
    from laya.claude_code.api import get_my_work

    out = await get_my_work(refresh=False)
    assert set(out) == {"github", "linear"}
