# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Find what the user's Claude Code login can read, and who the user is.

`discover()` locates the `claude` binary, lists the MCP servers and tools a
headless session gets (the `system/init` message, no model call), maps each
platform's roles (search, read thread, ...) onto the real tool names, and
reads the user's identity per platform (one small `claude -p` turn for the MCP
platforms, `gh api user` for GitHub). Tool names are never hardcoded: the
server prefix (`mcp__claude_ai_Slack__`, `mcp__linear-server__`, ...) depends
on how the user connected each service.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from laya.claude_code import cli
from laya.claude_code.store import LABELS, PLATFORMS
from laya.config import get_agent_binary

log = structlog.get_logger()

# Discovery spawns processes; the result is reused for this long.
CACHE_SECONDS = 3600

# Role -> accepted tool names (the part after `mcp__<server>__`), first match wins.
ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    "slack": {
        "search": ("slack_search_public_and_private", "slack_search_public"),
        "read_thread": ("slack_read_thread",),
        "read_channel": ("slack_read_channel",),
        "user_profile": ("slack_read_user_profile",),
        "send": ("slack_send_message",),
    },
    "gmail": {
        "search": ("search_threads",),
        "get_thread": ("get_thread",),
        "get_message": ("get_message",),
        "draft": ("create_draft",),
    },
    "linear": {
        "notifications": ("get_notifications",),
        "list_issues": ("list_issues",),
        "get_issue": ("get_issue",),
        "list_comments": ("list_comments",),
        "whoami": ("whoami",),
    },
    "notion": {
        "search": ("notion-search",),
        "fetch": ("notion-fetch",),
        "comments": ("notion-get-comments",),
        "self": ("notion-get-self",),
    },
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "slack": ("search", "read_thread", "read_channel"),
    "gmail": ("search", "get_thread"),
    "linear": ("notifications", "list_issues", "get_issue"),
    "notion": ("search", "fetch", "comments"),
}

# Roles a collector turn may use. Send/draft roles are never in this list.
READ_ROLES: dict[str, tuple[str, ...]] = {
    "slack": ("search", "read_thread", "read_channel", "user_profile"),
    "gmail": ("search", "get_thread", "get_message", "drive_read", "drive_meta"),
    "linear": ("notifications", "list_issues", "get_issue", "list_comments", "whoami"),
    "notion": ("search", "fetch", "comments", "self"),
}

SEND_ROLES: dict[str, str] = {"slack": "send", "gmail": "draft"}

# Gemini meeting notes link a Google Doc; these roles come from a Drive server.
_DRIVE_ROLES = {"drive_read": ("read_file_content",), "drive_meta": ("get_file_metadata",)}

# Words that identify a platform's server name when several servers offer a tool.
_SERVER_HINTS = {"slack": "slack", "gmail": "gmail", "linear": "linear", "notion": "notion"}


@dataclass
class PlatformDiscovery:
    available: bool = False
    tools: dict[str, str] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    problem: str | None = None
    server: str | None = None


@dataclass
class Discovery:
    cli: dict[str, Any] = field(default_factory=dict)
    platforms: dict[str, PlatformDiscovery] = field(default_factory=dict)
    # Every MCP tool name the session has; collector turns deny the ones
    # outside their allowlist.
    all_tools: list[str] = field(default_factory=list)
    at: float = 0.0
    # True once the identity turn ran (successful or not) for this result.
    identity_read: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"cli": self.cli, "platforms": {p: asdict(d) for p, d in self.platforms.items()}}


def normalize_server_name(name: str) -> str:
    """The server part of a tool name: `claude.ai Slack` -> `claude_ai_Slack`."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def split_tool(tool: str) -> tuple[str, str] | None:
    if not tool.startswith("mcp__"):
        return None
    rest = tool[len("mcp__"):]
    if "__" not in rest:
        return None
    server, name = rest.split("__", 1)
    return server, name


def _pick_server(platform: str, by_server: dict[str, set[str]], statuses: dict[str, str]) -> str | None:
    """The server that offers most of the platform's required roles."""
    roles = ROLES[platform]
    required = REQUIRED[platform]

    def covered(server: str) -> int:
        names = by_server[server]
        return sum(1 for r in required if any(n in names for n in roles[r]))

    candidates = [s for s in by_server if covered(s) > 0]
    if not candidates:
        return None
    hint = _SERVER_HINTS[platform]
    candidates.sort(key=lambda s: (
        -covered(s),
        statuses.get(s, "connected") != "connected",
        hint not in s.lower(),
        # The claude.ai connectors are the documented default.
        not s.startswith("claude_ai_"),
        s,
    ))
    return candidates[0]


def map_tools(
    tools: list[str], servers: list[dict[str, Any]] | None = None
) -> dict[str, PlatformDiscovery]:
    """Map each MCP platform's roles onto real tool names."""
    statuses = {normalize_server_name(s.get("name", "")): s.get("status", "") for s in servers or []}
    by_server: dict[str, set[str]] = {}
    for tool in tools:
        parts = split_tool(tool)
        if parts:
            by_server.setdefault(parts[0], set()).add(parts[1])

    result: dict[str, PlatformDiscovery] = {}
    for platform, roles in ROLES.items():
        found = PlatformDiscovery()
        server = _pick_server(platform, by_server, statuses)
        if server:
            names = by_server[server]
            for role, accepted in roles.items():
                name = next((n for n in accepted if n in names), None)
                if name:
                    found.tools[role] = f"mcp__{server}__{name}"
            found.server = server
        missing = [r for r in REQUIRED[platform] if r not in found.tools]
        if not missing:
            found.available = True
        else:
            found.problem = _missing_problem(platform, server, missing, statuses)
        result[platform] = found

    # Drive tools for the Gmail meeting notes (optional).
    drive = next((s for s in sorted(by_server) if "drive" in s.lower()), None)
    if drive:
        for role, accepted in _DRIVE_ROLES.items():
            name = next((n for n in accepted if n in by_server[drive]), None)
            if name:
                result["gmail"].tools[role] = f"mcp__{drive}__{name}"
    return result


def _missing_problem(platform: str, server: str | None, missing: list[str], statuses: dict[str, str]) -> str:
    label = LABELS[platform]
    hint = _SERVER_HINTS[platform]
    for name, status in statuses.items():
        if hint in name.lower() and status != "connected":
            return (
                f"The {label} connector in Claude Code is '{status}'. "
                f"Reconnect it in Claude Code (claude.ai Settings → Connectors, or /mcp)."
            )
    if server is None:
        return f"Claude Code has no {label} connector. Add one in claude.ai Settings → Connectors or with `claude mcp add`."
    return f"The {label} connector ({server}) lacks the tools: {', '.join(missing)}."


# ── identity ─────────────────────────────────────────────────────────────

_IDENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "slack": {"type": ["object", "null"], "properties": {
            "user_id": {"type": "string"}, "name": {"type": "string"}, "email": {"type": "string"}}},
        "gmail": {"type": ["object", "null"], "properties": {
            "email": {"type": "string"}, "name": {"type": "string"}}},
        "linear": {"type": ["object", "null"], "properties": {
            "id": {"type": "string"}, "email": {"type": "string"}, "name": {"type": "string"}}},
        "notion": {"type": ["object", "null"], "properties": {
            "id": {"type": "string"}, "email": {"type": "string"}, "name": {"type": "string"}}},
    },
}


def identity_prompt(platforms: dict[str, PlatformDiscovery]) -> tuple[str, list[str]]:
    """Prompt and read tools for one turn that reads the user's own identity."""
    steps: list[str] = []
    tools: list[str] = []
    t = {p: d.tools for p, d in platforms.items() if d.available}
    if "slack" in t and "user_profile" in t["slack"]:
        steps.append(f"slack: call {t['slack']['user_profile']} without a user id (it returns my own "
                     "profile); return my user_id (U…), real name and email.")
        tools.append(t["slack"]["user_profile"])
    if "gmail" in t:
        steps.append(f"gmail: call {t['gmail']['search']} with query \"in:sent\" and one result, then "
                     f"{t['gmail']['get_thread']} on it; return the From address and name of my message.")
        tools += [t["gmail"]["search"], t["gmail"]["get_thread"]]
    if "linear" in t and "whoami" in t["linear"]:
        steps.append(f"linear: call {t['linear']['whoami']}; return my id, email and name.")
        tools.append(t["linear"]["whoami"])
    if "notion" in t and "self" in t["notion"]:
        steps.append(f"notion: call {t['notion']['self']}; return my user id, email and name.")
        tools.append(t["notion"]["self"])
    prompt = (
        "You read the identity of the user running this session. Copy values exactly.\n"
        + "\n".join(f"- {s}" for s in steps)
        + "\nUse null for a platform you cannot read."
    )
    return prompt, tools


def identity_from_answer(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-platform identity dicts as stored in claude_code_sources.identity."""
    out: dict[str, dict[str, Any]] = {}
    slack = answer.get("slack") or {}
    if slack.get("user_id"):
        out["slack"] = {"slack_user_id": slack["user_id"], "name": slack.get("name") or "",
                        "email": (slack.get("email") or "").lower()}
    gmail = answer.get("gmail") or {}
    if gmail.get("email"):
        out["gmail"] = {"email": gmail["email"].lower(), "name": gmail.get("name") or ""}
    for p in ("linear", "notion"):
        item = answer.get(p) or {}
        if item.get("email") or item.get("id"):
            out[p] = {f"{p}_user_id": item.get("id") or "", "email": (item.get("email") or "").lower(),
                      "name": item.get("name") or ""}
    return out


async def read_mcp_identity(platforms: dict[str, PlatformDiscovery], deny: list[str]) -> dict[str, dict]:
    prompt, tools = identity_prompt(platforms)
    if not tools:
        return {}
    answer = await cli.run_collect("identity", prompt, _IDENTITY_SCHEMA, tools, deny=deny, timeout=300)
    return identity_from_answer(answer)


async def discover_github() -> PlatformDiscovery:
    found = PlatformDiscovery()
    try:
        user = await cli.run_gh("api", "user", timeout=30)
    except cli.ClaudeCodeError as e:
        found.problem = (
            "The GitHub CLI is not installed." if "not installed" in str(e)
            else "The GitHub CLI is not logged in. Run `gh auth login`."
        )
        return found
    found.available = True
    found.tools = {"cli": "gh"}
    found.identity = {
        "github_login": user.get("login") or "",
        "name": user.get("name") or "",
        "email": (user.get("email") or "").lower(),
    }
    return found


# ── entry point ──────────────────────────────────────────────────────────

_cache: Discovery | None = None
_lock = asyncio.Lock()


def cached() -> Discovery | None:
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None


async def discover(force: bool = False, with_identity: bool = True) -> Discovery:
    """Run discovery, or return the cached result when it is recent enough."""
    global _cache
    async with _lock:
        if (
            not force and _cache is not None
            and time.monotonic() - _cache.at < CACHE_SECONDS
            and (not with_identity or _cache.identity_read)
        ):
            return _cache

        result = Discovery(at=time.monotonic())
        binary = get_agent_binary("claude_code")
        version = await cli.cli_version(binary)
        result.cli = {"found": version is not None, "path": binary if version else None, "version": version}

        mcp: dict[str, PlatformDiscovery] = {
            p: PlatformDiscovery(problem="Claude Code CLI not found.") for p in ROLES
        }
        if version is not None:
            try:
                init = await cli.read_init()
                result.all_tools = [t for t in init.get("tools", []) if t.startswith("mcp__")]
                mcp = map_tools(result.all_tools, init.get("mcp_servers"))
            except cli.ClaudeCodeError as e:
                log.warning("claude_code_discovery_failed", error=str(e))
                mcp = {p: PlatformDiscovery(problem=f"Claude Code did not start: {e}") for p in ROLES}

            if with_identity and any(d.available for d in mcp.values()):
                try:
                    identities = await read_mcp_identity(mcp, result.all_tools)
                    for p, ident in identities.items():
                        mcp[p].identity = ident
                except cli.ClaudeCodeError as e:
                    log.warning("claude_code_identity_failed", error=str(e))
                for p, d in mcp.items():
                    if d.available and not d.identity:
                        d.problem = f"Could not read your {LABELS[p]} identity."

        result.identity_read = with_identity
        result.platforms = {**mcp, "github": await discover_github()}
        result.platforms = {p: result.platforms[p] for p in PLATFORMS}
        # Keep identities read earlier when this run skipped them.
        if not with_identity and _cache is not None:
            result.identity_read = _cache.identity_read
            for p, d in result.platforms.items():
                old = _cache.platforms.get(p)
                if old and old.identity and not d.identity:
                    d.identity = old.identity
        _cache = result
        return result
