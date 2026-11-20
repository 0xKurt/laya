# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""The model a collector turn runs on, per coding agent.

A source's ``config.model`` is ``agent/auto`` (Automatic, the default) or
``agent/<agent_id>/<model>`` (the stage-model form of llm/agent_backend.py).
A bare name (``haiku`` / ``sonnet`` / ``opus``) is a Claude Code model.

The login a sweep runs on is chosen per sweep (claude_code/accounts.py), so a
turn resolves the stored value against that login's agent: a model of the same
agent is used as is; Automatic, or a model of another agent, gives the agent's
light model under Automatic (``agent_backend.AUTO_ROLE_MODELS``), or none, which
lets the agent use its own default.

:func:`known_models` lists the models an agent offers in pickers: Claude Code's
model aliases, and for Codex the models the CLI itself lists in its model cache
(``models_cache.json`` in each Codex home).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

AUTOMATIC = "agent/auto"

CLAUDE_CODE_MODELS: list[dict[str, str]] = [
    {"value": "haiku", "label": "Haiku"},
    {"value": "sonnet", "label": "Sonnet"},
    {"value": "opus", "label": "Opus"},
]

# Gemini CLI keeps no local model list; these are the models its /model menu offers.
GEMINI_CLI_MODELS: list[dict[str, str]] = [
    {"value": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
    {"value": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    {"value": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite"},
]


def parse(value: str | None) -> tuple[str | None, str | None]:
    """(agent_id, model) of a stored value; (None, None) for Automatic."""
    text = (value or "").strip()
    if not text or text == AUTOMATIC or text.startswith(AUTOMATIC + "/"):
        return None, None
    if text.startswith("agent/"):
        from laya.llm.agent_backend import parse_agent_model_id

        try:
            return parse_agent_model_id(text)
        except ValueError:
            return None, None
    return "claude_code", text


def model_for(value: str | None, agent_id: str) -> str | None:
    """The ``--model`` of a turn on ``agent_id``; None = the agent's own default."""
    from laya.llm.agent_backend import AUTO_ROLE_MODELS

    chosen_agent, model = parse(value)
    if chosen_agent == agent_id and model:
        return model
    return AUTO_ROLE_MODELS.get(agent_id, {}).get("light")


def _codex_homes() -> list[Path]:
    from laya.claude_code.codex_accounts import HOMES_DIR

    homes = [Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")]
    if HOMES_DIR.is_dir():
        homes += sorted(p for p in HOMES_DIR.iterdir() if p.is_dir())
    return homes


def codex_models(homes: list[Path] | None = None) -> list[dict[str, str]]:
    """The models Codex lists for its logins (its own model cache), newest cache first."""
    caches = []
    for home in homes if homes is not None else _codex_homes():
        path = home / "models_cache.json"
        try:
            caches.append((path.stat().st_mtime, json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, data in sorted(caches, key=lambda c: c[0], reverse=True):
        models = data.get("models") if isinstance(data, dict) else None
        for m in models or []:
            if not isinstance(m, dict) or m.get("visibility", "list") != "list":
                continue
            slug = str(m.get("slug") or "").strip()
            if slug and slug not in seen:
                seen.add(slug)
                out.append({"value": slug, "label": str(m.get("display_name") or slug)})
    return out


def known_models(agent_id: str) -> list[dict[str, str]]:
    """Models a picker offers for ``agent_id`` (empty: only the agent's default)."""
    if agent_id == "claude_code":
        return [dict(m) for m in CLAUDE_CODE_MODELS]
    if agent_id == "codex_cli":
        return codex_models()
    if agent_id == "gemini_cli":
        return [dict(m) for m in GEMINI_CLI_MODELS]
    return []


async def choices() -> list[dict[str, Any]]:
    """Explicit collector models, grouped by agent, for the agents with a login
    collector turns can run on (``[{agent_id, label, models}]``)."""
    from laya.claude_code import accounts
    from laya.config import detect_agent_paths

    detected = detect_agent_paths()
    groups: list[dict[str, Any]] = []
    for agent_id, label in accounts.LOGIN_AGENTS.items():
        if not detected.get(agent_id):
            continue
        logins = accounts.login_module(agent_id)
        try:
            eligible = await logins.eligible_ids() if logins is not None else []
            if agent_id == accounts.AGENT_ID and eligible == [accounts.DEFAULT_ID]:
                eligible = eligible if (await accounts.default_status()).get("logged_in") else []
        except Exception:
            eligible = []
        if not eligible:
            continue
        models = [
            {"value": f"agent/{agent_id}/{m['value']}", "label": m["label"]}
            for m in known_models(agent_id)
        ]
        if models:
            groups.append({"agent_id": agent_id, "label": label, "models": models})
    return groups
