# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Linked documents enricher of the Claude Code connector."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from laya.claude_code import cli, discover, links, store
from laya.claude_code.discover import Discovery, PlatformDiscovery
from laya.claude_code.platforms import common


@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    """No test of the connector may spawn `claude` or `gh`."""

    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(cli, "_run_process", side_effect=_refuse), \
         patch.object(cli, "read_init", side_effect=_refuse), \
         patch.object(cli, "cli_version", side_effect=_refuse), \
         patch.object(cli, "run_gh", side_effect=_refuse):
        yield


@pytest.fixture(autouse=True)
def _tools():
    discover._cache = Discovery(platforms={
        "notion": PlatformDiscovery(available=True, tools={"fetch": "mcp__notion__notion-fetch",
                                                           "search": "mcp__notion__notion-search"}),
        "gmail": PlatformDiscovery(available=True, tools={"drive_read": "mcp__drive__read_file_content"}),
    })
    yield
    discover.reset_cache()


@pytest.fixture
def budget_open():
    with patch("laya.pipeline.agent_budget.is_agent_budget_paused", new_callable=AsyncMock,
               return_value=False):
        yield


NOTION = "https://www.notion.so/acme/Launch-plan-0123456789abcdef0123456789abcdef"
GDOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
PR = "https://github.com/acme/app/pull/42"


def _event(body, platform="slack", url="https://acme.slack.com/archives/C1/p1"):
    return common.make_event(
        platform=platform, native_id=body[:20], timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        raw_event_type="message_received", actor_name="Mike", actor_email="", actor_handle="U1",
        subject_type="thread", subject_id="C1", subject_title="#eng", subject_url=url,
        body=body, metadata={},
    )


def _source(platform="slack", **config):
    return {"platform": platform, "config": {**store.default_config(platform), **config},
            "deny_tools": ["mcp__notion__notion-create-pages"]}


def test_extract_links_kinds_order_dedupe_and_skip():
    text = (f"see <{NOTION}|the plan> and {GDOC} and {PR}, again {PR}#discussion "
            "and https://drive.google.com/open?id=FILE_ID_123 and https://example.com/x")
    found = links.extract_links(text)
    assert [link["kind"] for link in found] == ["notion", "google", "github", "google"]
    assert found[0]["id"] == "0123456789abcdef0123456789abcdef"
    assert found[0]["url"] == NOTION  # the Slack `|label` is not part of the link
    assert found[1]["id"] == "1AbCdEfGhIjKlMnOp"
    assert found[2]["key"] == "github:acme/app#42" and found[2]["id"] == "acme/app/42"
    assert found[3]["id"] == "FILE_ID_123"
    # The event's own page is not a linked document.
    own = links.extract_links(f"comment on {NOTION} about {PR}", skip=NOTION + "?pvs=4")
    assert [link["kind"] for link in own] == ["github"]
    dashed = links.extract_links("https://notion.so/0123abcd-0123-4567-89ab-0123456789ab")
    assert dashed[0]["id"] == "0123abcd0123456789ab0123456789ab"


@pytest.mark.asyncio
async def test_enrich_reads_github_and_batches_documents_in_one_turn(db, budget_open):
    gh = {"title": "Fix fees", "state": "closed", "pull_request": {"merged_at": "2026-01-01"},
          "body": "<!-- template -->\nRounds fees down.\n\nCloses #1."}
    docs = {"docs": [
        {"key": "notion:0123456789abcdef0123456789abcdef", "title": "Launch plan", "summary": "Launch on May 3."},
        {"key": "google:1AbCdEfGhIjKlMnOp", "title": "Budget", "summary": "Budget is 10k."},
    ]}
    event = _event(f"please check {NOTION} and {GDOC} and {PR} and https://github.com/acme/app/issues/7")
    with patch.object(cli, "run_gh", new_callable=AsyncMock, return_value=gh) as run_gh, \
         patch.object(cli, "run_collect", new_callable=AsyncMock, return_value=docs) as turn:
        [out] = await links.linked_documents([event], _source())
    # At most three links per event: the issue (4th link) is left out.
    assert run_gh.await_count == 1
    assert run_gh.await_args.args == ("api", "repos/acme/app/issues/42")
    assert turn.await_count == 1
    args, kwargs = turn.await_args.args, turn.await_args.kwargs
    assert args[0] == "links"
    assert args[3] == ["mcp__drive__read_file_content", "mcp__notion__notion-fetch"]
    assert kwargs["deny"] == ["mcp__notion__notion-create-pages"] and kwargs["model"] == "haiku"
    body = out.content.body
    section = body.split("\n\nLinked documents:\n")[1]
    assert section.splitlines() == [
        f"- Launch plan ({NOTION}): Launch on May 3.",
        f"- Budget ({GDOC}): Budget is 10k.",
        f"- PR acme/app#42: Fix fees ({PR}): Merged. Rounds fees down. Closes #1.",
    ]
    assert out.content.metadata["linked_documents"] == [NOTION, GDOC, PR]

    # Cached for 24 h: the same links in a new event cost no call.
    again = _event(f"{PR} {NOTION}")
    with patch.object(cli, "run_collect", new_callable=AsyncMock) as turn:
        [out2] = await links.linked_documents([again], _source())
    turn.assert_not_awaited()
    assert "Launch on May 3." in out2.content.body and "Merged." in out2.content.body


@pytest.mark.asyncio
async def test_enrich_cache_expires(db, budget_open):
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    await store.set_state("links", "link:google:1AbCdEfGhIjKlMnOp",
                          json.dumps({"at": old, "title": "Old", "summary": "stale"}))
    docs = {"docs": [{"key": "google:1AbCdEfGhIjKlMnOp", "title": "Budget", "summary": "fresh"}]}
    with patch.object(cli, "run_collect", new_callable=AsyncMock, return_value=docs) as turn:
        [out] = await links.linked_documents([_event(GDOC)], _source("gmail"))
    turn.assert_awaited_once()
    assert "fresh" in out.content.body and "stale" not in out.content.body


@pytest.mark.asyncio
async def test_enrich_skips_model_turn_when_budget_paused(db):
    gh = {"title": "Bug", "state": "open", "body": "Crashes on start."}
    with patch("laya.pipeline.agent_budget.is_agent_budget_paused", new_callable=AsyncMock, return_value=True), \
         patch.object(cli, "run_gh", new_callable=AsyncMock, return_value=gh), \
         patch.object(cli, "run_collect", new_callable=AsyncMock) as turn:
        [out] = await links.linked_documents([_event(f"{NOTION} {PR}")], _source())
    turn.assert_not_awaited()
    assert "Open. Crashes on start." in out.content.body and NOTION not in out.content.body.split("Linked")[1]
    # Not cached: the next sweep with an open budget reads it.
    assert "link:notion:0123456789abcdef0123456789abcdef" not in await store.get_state("links", "link:")


@pytest.mark.asyncio
async def test_enrich_switch_platforms_and_failures(db, budget_open):
    event = _event(f"see {NOTION}")
    with patch.object(cli, "run_collect", new_callable=AsyncMock) as turn:
        assert (await links.linked_documents([event], _source(linked_documents=False)))[0].content.body == f"see {NOTION}"
        gh_event = _event(f"see {NOTION}", platform="github")
        assert (await links.linked_documents([gh_event], {"platform": "github", "config": {}}))[0] is gh_event
    turn.assert_not_awaited()

    # A failed turn leaves the event as it was and caches nothing.
    with patch.object(cli, "run_collect", new_callable=AsyncMock, side_effect=cli.ClaudeCodeError("boom")):
        [out] = await links.linked_documents([event], _source())
    assert out.content.body == f"see {NOTION}"
    assert await store.get_state("links", "link:") == {}

    # An unreadable document is cached as such and adds no line.
    with patch.object(cli, "run_collect", new_callable=AsyncMock, return_value={"docs": []}):
        [out] = await links.linked_documents([event], _source())
    assert "Linked documents" not in out.content.body
    assert "link:notion:0123456789abcdef0123456789abcdef" in await store.get_state("links", "link:")


@pytest.mark.asyncio
async def test_enrich_without_doc_tools_uses_no_turn(db, budget_open):
    discover.reset_cache()
    with patch.object(cli, "run_collect", new_callable=AsyncMock) as turn:
        [out] = await links.linked_documents([_event(f"see {NOTION}")], _source())
    turn.assert_not_awaited()
    assert out.content.body == f"see {NOTION}"


def test_linked_documents_default_on_for_collecting_platforms():
    for platform in links.PLATFORMS:
        assert store.default_config(platform)["linked_documents"] is True
    assert "linked_documents" not in store.default_config("github")
    assert links.linked_documents in common.ENRICHERS


def test_extract_links_app_notion_com():
    text = "<https://app.notion.com/p/acme/Venue-Program-41db7df18a6b8174a1e6fb38a7c6dcba?source=copy_link|x>"
    [link] = links.extract_links(text)
    assert link["kind"] == "notion" and link["id"] == "41db7df18a6b8174a1e6fb38a7c6dcba"


def test_github_excerpt_skips_markdown_noise():
    text = "<!-- tpl -->\n## What\nLists 9 perps.\n\n| a | b |\n|---|---|\n```\ncode\n```\nDone."
    assert links._excerpt(text) == "What Lists 9 perps. code Done."
