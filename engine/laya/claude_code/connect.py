# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Connect, disconnect and configure a Claude Code platform.

Connecting a platform, all idempotent:

1. creates the `sources` row (`src_claude_code_<platform>`, workflow id
   `claude_code:<platform>`, source type `claude_code`) before the first
   sweep, so `space_resolution` maps the events to the chosen space;
2. stores the discovered tools and identity in `claude_code_sources`;
3. writes the user's handles into team.json `self.accounts` and emails into
   `self.aliases`, so `run_ingest` resolves the user's own messages to
   `actor_relationship == "self"`;
4. seeds the platform's filter rules into rules.json by name;
5. runs the registered connect hooks (e.g. creating a send connection).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from laya.claude_code import store
from laya.claude_code.discover import Discovery, PlatformDiscovery, discover
from laya.claude_code.service import source_id
from laya.config import load_rules, load_settings, load_team, save_rules, save_settings, save_team
from laya.db.sqlite import get_db
from laya.models.rules import RulesConfig
from laya.models.team import TeamConfig

log = structlog.get_logger()


class ConnectError(Exception):
    """The platform cannot be connected; the message says why."""


# Hooks run after a platform was connected / before it is disconnected, with
# (platform, discovery of that platform). Sending registers its connection here.
ConnectHook = Callable[[str, PlatformDiscovery], Awaitable[None]]
DisconnectHook = Callable[[str], Awaitable[None]]
CONNECT_HOOKS: list[ConnectHook] = []
DISCONNECT_HOOKS: list[DisconnectHook] = []

RULE_GITHUB = "Claude Code · GitHub: ignore review requests, state changes and CI"
RULE_BOTS = "Claude Code · Ignore bot comments"
RULE_LINEAR = "Claude Code · Linear: ignore review requests"

# name -> (platforms that need it, rule). A rule needed by several platforms is
# removed when the last of them is disconnected.
SEEDED_RULES: dict[str, tuple[frozenset[str], dict[str, Any]]] = {
    RULE_GITHUB: (frozenset({"github"}), {
        "name": RULE_GITHUB,
        "enabled": True,
        "condition": {"all": [
            {"field": "source.platform", "operator": "equals", "value": "github"},
            {"field": "source.raw_event_type", "operator": "in",
             "value": ["review_requested", "state_change", "ci_activity"]},
        ]},
        "action": "drop",
    }),
    RULE_BOTS: (frozenset(store.PLATFORMS), {
        "name": RULE_BOTS,
        "enabled": True,
        "condition": {"field": "actor.platform_handle", "operator": "ends_with", "value": "[bot]"},
        "action": "drop",
    }),
    RULE_LINEAR: (frozenset({"linear"}), {
        "name": RULE_LINEAR,
        "enabled": True,
        "condition": {"all": [
            {"field": "source.platform", "operator": "equals", "value": "linear"},
            {"field": "source.raw_event_type", "operator": "equals", "value": "review_requested"},
        ]},
        "action": "drop",
    }),
}


# ── team.json identity ───────────────────────────────────────────────────


def identity_handles(identity: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(accounts, emails) a platform identity contributes to team.json `self`."""
    accounts = [identity[k] for k in ("slack_user_id", "github_login") if identity.get(k)]
    emails = [identity["email"].lower()] if identity.get("email") else []
    return accounts, emails


def merge_identity_into_team(team: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """Add the identity's handles to the `self` member; other members are untouched.

    Creates the `self` member from the identity when team.json has none.
    """
    accounts, emails = identity_handles(identity)
    members = [dict(m) for m in team.get("members", [])]
    me = next((m for m in members if m.get("role") == "self"), None)
    if me is None:
        me = {
            "name": identity.get("name") or "Me",
            "email": emails[0] if emails else "",
            "role": "self",
            "notes": "",
            "aliases": [],
            "accounts": [],
        }
        members.append(me)
    if not me.get("email") and emails:
        me["email"] = emails[0]
    known_accounts = {a.lower() for a in me.get("accounts") or []}
    me["accounts"] = list(me.get("accounts") or []) + [
        a for a in accounts if a.lower() not in known_accounts
    ]
    known_emails = {e.lower() for e in [me.get("email") or "", *(me.get("aliases") or [])]}
    me["aliases"] = list(me.get("aliases") or []) + [e for e in emails if e not in known_emails]
    # The same validation PUT /team applies.
    return TeamConfig(**{**team, "members": members}).model_dump(mode="json")


def write_identity(identity: dict[str, Any]) -> None:
    team = load_team()
    updated = merge_identity_into_team(team, identity)
    if updated != team:
        save_team(updated)
        log.info("claude_code_team_identity_written")


# ── rules.json ───────────────────────────────────────────────────────────


def seed_rules(platform: str) -> list[str]:
    """Add the platform's rules that are missing by name; returns the added names."""
    rules = load_rules()
    present = {r.get("name") for r in rules.get("rules", [])}
    added = [
        name for name, (platforms, _) in SEEDED_RULES.items()
        if platform in platforms and name not in present
    ]
    if added:
        rules = {**rules, "rules": [*rules.get("rules", []), *(SEEDED_RULES[n][1] for n in added)]}
        save_rules(RulesConfig(**rules).model_dump(mode="json"))
        log.info("claude_code_rules_seeded", platform=platform, rules=added)
    return added


def remove_rules(platform: str, still_connected: set[str]) -> list[str]:
    """Remove the seeded rules no remaining connected platform needs."""
    rules = load_rules()
    drop = {
        name for name, (platforms, _) in SEEDED_RULES.items()
        if platform in platforms and not (platforms & still_connected)
    }
    kept = [r for r in rules.get("rules", []) if r.get("name") not in drop]
    removed = [r.get("name") for r in rules.get("rules", []) if r.get("name") in drop]
    if removed:
        save_rules({**rules, "rules": kept})
        log.info("claude_code_rules_removed", platform=platform, rules=removed)
    return removed


# ── sources row ──────────────────────────────────────────────────────────


async def ensure_source_row(platform: str, space_id: str | None) -> str:
    """Create or update the platform's `sources` row; returns its space id."""
    db = await get_db()
    if space_id:
        rows = await db.execute_fetchall("SELECT 1 FROM spaces WHERE space_id = ?", (space_id,))
        if not rows:
            raise ConnectError(f"Space '{space_id}' does not exist")
    sid = source_id(platform)
    existing = await db.execute_fetchall("SELECT space_id FROM sources WHERE source_id = ?", (sid,))
    if existing:
        space_id = space_id or existing[0]["space_id"] or "default"
        await db.execute("UPDATE sources SET space_id = ? WHERE source_id = ?", (space_id, sid))
    else:
        space_id = space_id or "default"
        # workflow_id is UNIQUE: an event delivered before connect may have
        # auto-registered a row for it under a random id.
        await db.execute("DELETE FROM sources WHERE workflow_id = ?", (f"claude_code:{platform}",))
        await db.execute(
            """INSERT INTO sources (source_id, name, platform, workflow_id, space_id, source_type)
               VALUES (?, ?, ?, ?, ?, 'claude_code')""",
            (sid, f"{store.LABELS[platform]} (Claude Code)", platform,
             f"claude_code:{platform}", space_id),
        )
    await db.commit()
    return space_id


# ── connect / disconnect / configure ─────────────────────────────────────


# Stage models on the user's subscription logins ("Automatic": whichever coding
# agent login has room), used when no API key is configured: without a key an API
# model cannot process a single event, so a user who connects through a coding
# agent would get no cards at all.
SUBSCRIPTION_MODELS = {role: "agent/auto" for role in ("router", "stager", "omni", "chat", "trace")}


def _has_llm_key() -> bool:
    from laya.security.keychain import has_api_key

    providers = ("anthropic", "openai", "google", "openrouter")
    return any(has_api_key(p) for p in providers) or bool(load_settings().get("custom_providers"))


def use_subscription_models() -> list[str]:
    """Point the stage models at Automatic (subscription logins) when there is no API key.

    Only roles still on an API model change; agent and local models the user
    chose stay. Returns the roles changed (visible in Settings → Models).
    """
    if _has_llm_key():
        return []
    settings = load_settings()
    models = dict(settings.get("models") or {})
    changed = [
        role for role in SUBSCRIPTION_MODELS
        if not str(models.get(role, "")).startswith(("agent/", "ollama/", "lm_studio/"))
    ]
    if changed:
        models.update({role: SUBSCRIPTION_MODELS[role] for role in changed})
        settings["models"] = models
        save_settings(settings)
        log.info("claude_code_subscription_models_set", roles=changed)
    return changed


async def connect(platform: str, space_id: str | None = None, found: Discovery | None = None) -> None:
    if platform not in store.PLATFORMS:
        raise ConnectError(f"Unknown platform '{platform}'")
    found = found or await discover(with_identity=True)
    pd = found.platforms.get(platform)
    if pd is None or not pd.available:
        raise ConnectError((pd.problem if pd else None) or f"{store.LABELS[platform]} is not available")
    if not pd.identity:
        raise ConnectError(pd.problem or f"Could not read your {store.LABELS[platform]} identity")

    await ensure_source_row(platform, space_id)
    existing = await store.get_source(platform)
    fields: dict[str, Any] = {"tools": pd.tools, "identity": pd.identity, "enabled": True}
    if existing is None:
        fields.update(config=store.default_config(platform), connected_at=store.now_iso())
    await store.upsert_source(platform, **fields)
    write_identity(pd.identity)
    seed_rules(platform)
    use_subscription_models()
    for hook in CONNECT_HOOKS:
        await hook(platform, pd)
    log.info("claude_code_connected", platform=platform)


async def disconnect(platform: str) -> None:
    if platform not in store.PLATFORMS:
        raise ConnectError(f"Unknown platform '{platform}'")
    for hook in DISCONNECT_HOOKS:
        await hook(platform)
    db = await get_db()
    await db.execute("DELETE FROM sources WHERE source_id = ?", (source_id(platform),))
    await db.commit()
    await store.delete_source(platform)
    remaining = {s["platform"] for s in await store.list_sources()}
    remove_rules(platform, remaining)
    log.info("claude_code_disconnected", platform=platform)


def validate_config(platform: str, config: dict[str, Any]) -> dict[str, Any]:
    """The full config for a platform: known keys only, types checked."""
    defaults = store.default_config(platform)
    unknown = sorted(set(config) - set(defaults))
    if unknown:
        raise ConnectError(f"Unknown config keys: {', '.join(unknown)}")
    merged = store.merged_config(platform, config)
    for key, default in defaults.items():
        value = merged[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        elif isinstance(default, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        elif isinstance(default, dict):
            ok = isinstance(value, dict)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ConnectError(f"Invalid value for '{key}'")
    hours = merged["active_hours"]
    days = hours.get("days")
    if not (isinstance(days, list) and all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
        raise ConnectError("active_hours.days must be weekday numbers 0-6")
    for k in ("start", "end"):
        v = hours.get(k)
        if not (isinstance(v, str) and len(v) == 5 and v[2] == ":" and v.replace(":", "").isdigit()):
            raise ConnectError(f"active_hours.{k} must be HH:MM")
    if merged["timezone"]:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(merged["timezone"])
        except Exception as e:
            raise ConnectError(f"Unknown timezone '{merged['timezone']}'") from e
    return merged


async def configure(platform: str, config: dict[str, Any]) -> dict[str, Any]:
    if await store.get_source(platform) is None:
        raise ConnectError(f"{store.LABELS.get(platform, platform)} is not connected")
    merged = validate_config(platform, config)
    await store.upsert_source(platform, config=merged)
    return merged
