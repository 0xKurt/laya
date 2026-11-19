# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Effort estimates, session prep and session-claim verification
(docs/claude-code-connector.md, "Effort and session lane")."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from laya.llm.client import LLMResponse
from laya.llm.prompts.stager import EFFORT_RULES, build_stager_messages, get_stager_json_schema
from laya.models.card import CardResponse, StagedOutput
from laya.pipeline.stager import _parse_stager_response
from laya.sessions import prep, verify
from laya.sessions.effort import format_effort, parse_effort
from tests.conftest import insert_test_card


async def _client():
    from laya.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _llm(parsed: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(parsed), parsed=parsed, model="m",
        input_tokens=1, output_tokens=1, latency_ms=1,
    )


# ── effort: prompt gating, schema, parsing ──


def test_effort_rules_and_schema_only_when_enabled(sample_event):
    from laya.models.classification import RouterOutput

    router = RouterOutput(
        category="CODE", persona="ENGINEER", priority="MEDIUM", confidence=0.9,
        reasoning="r", requires_research=False,
    )
    off = build_stager_messages(sample_event, router, card_status=False)
    on = build_stager_messages(sample_event, router, card_status=False, effort=True)
    assert "## Effort" not in off[0]["content"]
    assert EFFORT_RULES in on[0]["content"]

    assert "effort" not in get_stager_json_schema(card_status=False)["schema"]["properties"]
    schema = get_stager_json_schema(card_status=False, effort=True)["schema"]
    assert "effort" in schema["required"]
    assert schema["properties"]["effort"]["required"] == ["basis", "you_minutes", "agent_minutes"]


def test_parse_effort_validates_and_clamps():
    assert parse_effort({"you_minutes": 30, "agent_minutes": 120, "basis": " fix +  tests "}).model_dump() == {
        "you_minutes": 30, "agent_minutes": 120, "basis": "fix + tests", "set_by": "stager",
    }
    assert parse_effort({"you_minutes": -5, "agent_minutes": 10**9}).you_minutes == 0
    assert parse_effort({"you_minutes": -5, "agent_minutes": 10**9}).agent_minutes == 2400
    assert parse_effort({"you_minutes": True, "agent_minutes": 1}) is None
    assert parse_effort({"you_minutes": 5}) is None
    assert parse_effort("30m") is None


def test_format_effort():
    effort = parse_effort({"you_minutes": 30, "agent_minutes": 120})
    assert format_effort(effort) == "you ~30m · agent ~2h"
    assert format_effort(parse_effort({"you_minutes": 90, "agent_minutes": 0})) == "you ~1h30m · agent ~0m"


def test_stager_response_carries_effort_into_staged_output(sample_event):
    data = {
        "header": "h", "summary": "s", "intelligence_report": [],
        "staged_output": {"type": "summary", "content": "c"},
        "suggested_actions": [], "privacy_tier": 2, "suggested_tags": [],
        "effort": {"you_minutes": 15, "agent_minutes": 60, "basis": "small fix"},
    }
    card = _parse_stager_response(data, sample_event)
    assert card.staged_output.effort.you_minutes == 15
    stored = json.loads(json.dumps(card.staged_output.model_dump()))
    assert stored["effort"]["agent_minutes"] == 60

    del data["effort"]
    assert _parse_stager_response(data, sample_event).staged_output.effort is None


# ── effort: API ──


@pytest.mark.asyncio
async def test_effort_correction_is_stored_and_shown_on_slim_board(db):
    await insert_test_card(db, card_id="card_c1", event_id="e1", status="ready")
    async with await _client() as client:
        resp = await client.put("/cards/card_c1/effort", json={"you_minutes": 45, "agent_minutes": 90, "basis": "mine"})
        assert resp.status_code == 200
        board = (await client.get("/board")).json()["cards"]
        detail = (await client.get("/cards/card_c1")).json()
        missing = await client.put("/cards/nope/effort", json={"you_minutes": 1, "agent_minutes": 1})

    assert board[0]["effort"] == {"you_minutes": 45, "agent_minutes": 90, "basis": "mine", "set_by": "user"}
    assert board[0]["staged_output"] is None
    # The rest of staged_output is kept.
    assert detail["staged_output"]["content"] == "Add null check"
    assert detail["effort"]["set_by"] == "user"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_agent_result_keeps_effort(db):
    from laya.sessions.effort import staged_output_keeping_effort

    await insert_test_card(db, card_id="card_c1", event_id="e1")
    await db.execute(
        "UPDATE action_cards SET staged_output = ? WHERE card_id = 'card_c1'",
        (json.dumps({"type": "summary", "content": "x", "effort": {"you_minutes": 5, "agent_minutes": 6}}),),
    )
    out = json.loads(await staged_output_keeping_effort(db, "card_c1", {"type": "agent_result", "content": "done"}))
    assert out == {"type": "agent_result", "content": "done", "effort": {"you_minutes": 5, "agent_minutes": 6}}


# ── session prep ──


def _card(**kw) -> CardResponse:
    base = dict(
        card_id="card_1", event_id="e1", priority="HIGH", persona="ENGINEER", category="CODE",
        header="Fix signer rotation in dashboard", summary="The signer key rotation fails for the MM team.",
        intelligence=["See https://linear.app/somnia/issue/TRAD-590/x", "Safe 0x1111111111111111111111111111111111111111"],
        entity_id="github:pull_request:somnia-chain/somnia-dashboard#882",
        source_url="https://github.com/somnia-chain/somnia-dashboard/pull/882",
    )
    base.update(kw)
    return CardResponse(**base)


def test_repo_detection_sources():
    assert prep.github_repo_of(_card()) == "somnia-chain/somnia-dashboard"
    linear = _card(entity_id="linear:ticket:TRAD-590", source_url=None,
                   summary="see https://github.com/somnia-chain/contracts/blob/main/x.sol")
    assert prep.github_repo_of(linear) == "somnia-chain/contracts"
    assert prep.github_repo_of(_card(entity_id="slack:thread:C1", source_url=None, intelligence=[]),
                               {"github_repo": "o/r"}) == "o/r"
    assert prep.github_repo_of(_card(entity_id="slack:thread:C1", source_url=None, intelligence=[])) is None


def test_topic_slug():
    assert prep.slugify_topic("TRAD-590 Move every contract to its owner Safe!") == "trad-590-move-every-contract-to-its"
    assert prep.fallback_topic(_card()) == "trad-590-fix-signer-rotation-in"
    assert prep.slugify_topic("") == "session"


@pytest.mark.asyncio
async def test_prepare_session_with_local_checkout(tmp_path):
    checkout = tmp_path / "somnia-dashboard"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".env.local").write_text("X=1")
    (checkout / "package-lock.json").write_text("{}")

    async def fake_run(*args):
        if "symbolic-ref" in args:
            return 0, "origin/develop"
        if "ls-files" in args:
            return 0, ""  # untracked
        return 1, ""

    llm = AsyncMock(return_value=_llm({"topic": "Signer Rotation Fix", "prompt": "the signer rotation is broken."}))
    with patch.object(prep, "checkout_roots", return_value=[tmp_path]), \
         patch.object(prep, "_configured_repos", return_value=[]), \
         patch.object(prep, "_run", side_effect=fake_run), \
         patch("laya.llm.client.llm_call", llm):
        result = await prep.prepare_session(_card())

    assert result.topic == "signer-rotation-fix"
    assert result.prompt.endswith(prep.CLOSING_LINE)
    assert result.repo["default_branch"] == "develop"
    assert result.worktree_path == str(tmp_path / "somnia-dashboard-signer-rotation-fix")
    assert result.commands[0] == (
        f"git -C {checkout} fetch origin && git -C {checkout} worktree add "
        "../somnia-dashboard-signer-rotation-fix -b signer-rotation-fix origin/develop"
    )
    assert f"cp {checkout}/.env.local {result.worktree_path}/" in result.commands
    assert f"cd {result.worktree_path} && npm ci" in result.commands
    # The card's facts go to the model in full.
    user_msg = llm.call_args.kwargs["messages"][1]["content"]
    assert "0x1111111111111111111111111111111111111111" in user_msg
    assert "TRAD-590" in user_msg
    assert llm.call_args.kwargs["role"] == "stager"


@pytest.mark.asyncio
async def test_prepare_session_without_checkout_clones_and_survives_llm_failure(tmp_path):
    async def fake_run(*args):
        return 1, ""

    with patch.object(prep, "checkout_roots", return_value=[tmp_path]), \
         patch.object(prep, "_configured_repos", return_value=[]), \
         patch.object(prep, "_run", side_effect=fake_run), \
         patch.object(prep.shutil, "which", return_value=None), \
         patch("laya.llm.client.llm_call", AsyncMock(side_effect=RuntimeError("down"))):
        result = await prep.prepare_session(_card())

    assert result.llm_error == "down"
    assert result.commands[0] == (
        f"git clone git@github.com:somnia-chain/somnia-dashboard.git {tmp_path / 'somnia-dashboard'}"
    )
    assert "origin/main" in result.commands[1]
    assert "https://github.com/somnia-chain/somnia-dashboard/pull/882" in result.prompt
    assert result.prompt.endswith(prep.CLOSING_LINE)


@pytest.mark.asyncio
async def test_prepare_session_uses_repos_config(tmp_path):
    checkout = tmp_path / "elsewhere"
    checkout.mkdir()
    repos = [{"name": "contracts", "path": str(checkout), "remote_id": "somnia-chain/contracts"}]
    card = _card(entity_id="linear:ticket:TRAD-1", source_url=None, intelligence=[],
                 summary="the contracts repo needs a fix")

    with patch.object(prep, "_configured_repos", return_value=repos), \
         patch.object(prep, "_run", AsyncMock(return_value=(1, ""))), \
         patch.object(prep.shutil, "which", return_value=None):
        target = await prep.resolve_repo(card)

    assert target.full_name == "somnia-chain/contracts"
    assert target.checkout == str(checkout)
    assert target.found_by == "repos_config"


@pytest.mark.asyncio
async def test_prepare_session_without_repo():
    card = _card(entity_id="slack:thread:C1", source_url=None, intelligence=[])
    with patch.object(prep, "_configured_repos", return_value=[]), \
         patch("laya.llm.client.llm_call", AsyncMock(return_value=_llm({"topic": "t", "prompt": "p. get it green."}))):
        result = await prep.prepare_session(card)
    assert result.commands == []
    assert result.repo is None
    assert result.prompt == "p. get it green."


@pytest.mark.asyncio
async def test_session_prep_endpoint(db):
    await insert_test_card(db, card_id="card_c1", event_id="e1", entity_id="slack:thread:C1")
    with patch.object(prep, "_configured_repos", return_value=[]), \
         patch("laya.llm.client.llm_call", AsyncMock(return_value=_llm({"topic": "t", "prompt": "do it"}))):
        async with await _client() as client:
            resp = await client.post("/cards/card_c1/session-prep")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"].startswith("do it")
    assert body["commands"] == []


# ── session claims ──


def test_extract_claims_by_line():
    text = (
        "Opened https://github.com/o/r/pull/12 and it is merged.\n"
        "CI is green on o/r#13\n"
        "PR #14 merged too\n"
        "deployed to testnet\n"
        "nothing here #15"
    )
    claims = {(c.repo, c.number): c.claims for c in verify.extract_claims(text, default_repo="o/r")}
    assert claims == {
        ("o/r", 12): ["merged"],
        ("o/r", 13): ["green"],
        ("o/r", 14): ["merged"],
        (None, None): ["deployed"],
    }
    assert verify.extract_claims("PR #14 merged")[0].repo is None


def test_checks_state():
    assert verify.checks_state([]) == "none"
    assert verify.checks_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "green"
    assert verify.checks_state([{"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert verify.checks_state([{"status": "COMPLETED", "conclusion": "FAILURE"}]) == "failing"
    assert verify.checks_state([{"state": "PENDING"}]) == "pending"


@pytest.mark.asyncio
async def test_verify_claims_against_gh():
    async def fake_gh(repo, number):
        if number == 1:
            return {"state": "MERGED", "url": "u1", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}
        if number == 2:
            return {"state": "OPEN", "url": "u2", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}
        raise RuntimeError("gh is not installed")

    text = "o/r#1 merged and green\no/r#2 merged, checks passing\no/r#3 merged"
    with patch.object(verify, "_gh_pr_view", side_effect=fake_gh):
        results = {c["number"]: c for c in await verify.verify_claims(text)}

    assert results[1]["results"] == {"merged": "confirmed", "green": "confirmed"}
    assert results[2]["results"] == {"merged": "contradicted", "green": "contradicted"}
    assert results[3]["results"] == {"merged": "unverified"}
    assert results[3]["error"] == "gh is not installed"


@pytest.mark.asyncio
async def test_session_claims_endpoint(db):
    await insert_test_card(db, card_id="card_c1", event_id="e1", entity_id="github:pull_request:o/r#7")
    await db.execute(
        "UPDATE action_cards SET staged_output = ? WHERE card_id = 'card_c1'",
        (json.dumps({"type": "agent_result", "content": "PR #7 is merged and CI is green"}),),
    )
    await db.commit()
    await insert_test_card(db, card_id="card_c2", event_id="e2")

    gh = AsyncMock(return_value={"state": "OPEN", "url": "u", "statusCheckRollup": []})
    with patch.object(verify, "_gh_pr_view", gh):
        async with await _client() as client:
            claims = (await client.get("/cards/card_c1/session-claims")).json()["claims"]
            none = (await client.get("/cards/card_c2/session-claims")).json()["claims"]

    gh.assert_awaited_once_with("o/r", 7)
    assert claims[0]["results"] == {"merged": "contradicted", "green": "unverified"}
    assert none == []
