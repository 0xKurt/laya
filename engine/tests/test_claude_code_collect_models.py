# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

import json
from unittest.mock import AsyncMock, patch

import pytest

from laya.claude_code import cli, collect_models


@pytest.mark.parametrize("value,expected", [
    ("agent/auto", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
    ("haiku", ("claude_code", "haiku")),
    ("agent/claude_code/opus", ("claude_code", "opus")),
    ("agent/codex_cli/gpt-x", ("codex_cli", "gpt-x")),
])
def test_parse(value, expected):
    assert collect_models.parse(value) == expected


def test_model_for_resolves_per_agent():
    # A stored bare name keeps meaning Claude Code's model.
    assert collect_models.model_for("sonnet", "claude_code") == "sonnet"
    assert collect_models.model_for("sonnet", "codex_cli") is None
    # Automatic: Claude Code's light model, Codex's own default.
    assert collect_models.model_for("agent/auto", "claude_code") == "haiku"
    assert collect_models.model_for("agent/auto", "codex_cli") is None
    assert collect_models.model_for("agent/codex_cli/gpt-x", "codex_cli") == "gpt-x"
    assert collect_models.model_for("agent/codex_cli/gpt-x", "claude_code") == "haiku"


def test_codex_models_reads_listed_models_of_the_cache(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "models_cache.json").write_text(json.dumps({"models": [{"slug": "m-old", "visibility": "list"}]}))
    (new / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "m-a", "display_name": "M A", "visibility": "list"},
        {"slug": "m-hidden", "visibility": "hide"},
        {"slug": "m-old", "visibility": "list"},
    ]}))
    import os
    os.utime(old / "models_cache.json", (1, 1))
    assert collect_models.codex_models([old, new, tmp_path / "missing"]) == [
        {"value": "m-a", "label": "M A"},
        {"value": "m-old", "label": "m-old"},
    ]


def test_store_default_is_automatic():
    from laya.claude_code import store

    assert store.default_config("slack")["model"] == "agent/auto"
    # An existing stored model is kept as is.
    assert store.merged_config("slack", {"model": "haiku"})["model"] == "haiku"


@pytest.mark.asyncio
async def test_claude_turn_runs_the_resolved_model():
    seen = {}

    async def fake_run(args, timeout, env=None):
        seen["model"] = args[args.index("--model") + 1]
        return ['{"type":"result","structured_output":{"ok":true},"usage":{}}']

    with patch.object(cli, "_run_process", fake_run), \
         patch("laya.llm.client.log_to_audit", AsyncMock()), \
         patch("laya.claude_code.accounts.record", AsyncMock()):
        await cli.run_collect("linear", "p", {"type": "object"}, ["mcp__x__y"])
        assert seen["model"] == "haiku"
        await cli.run_collect("linear", "p", {"type": "object"}, ["mcp__x__y"], model="agent/claude_code/opus")
        assert seen["model"] == "opus"
        await cli.run_collect("linear", "p", {"type": "object"}, ["mcp__x__y"], model="sonnet")
        assert seen["model"] == "sonnet"


@pytest.mark.asyncio
async def test_choices_only_for_agents_with_a_login():
    with patch("laya.config.detect_agent_paths", return_value={"claude_code": "/bin/claude", "codex_cli": "/bin/codex"}), \
         patch("laya.claude_code.accounts.eligible_ids", AsyncMock(return_value=["acc1"])), \
         patch("laya.claude_code.codex_accounts.eligible_ids", AsyncMock(return_value=[])):
        groups = await collect_models.choices()
    assert [g["agent_id"] for g in groups] == ["claude_code"]
    assert groups[0]["models"][0] == {"value": "agent/claude_code/haiku", "label": "Haiku"}
