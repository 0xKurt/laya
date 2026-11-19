# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Short-lived MCP bearer tokens for one agent inference turn.

When chat or Coherence runs on an agent model (``agent/claude_code/...``), the agent
runs the tool loop itself and calls Laya's tools through Laya's own Streamable HTTP
MCP endpoint. Each such turn gets its own token, minted here, that:

- is valid only until it expires or the turn revokes it;
- opens only the Streamable HTTP transport (``/mcp/``), not the legacy SSE one;
- limits the MCP session it opens to an explicit set of tool names, independent of
  the user's Settings → MCP scope toggles (those govern external clients; the set
  here is what Laya's own chat would have offered for the turn, cut to read tools).

Tokens live in process memory only; an engine restart invalidates all of them.
"""

from __future__ import annotations

import hmac
import secrets
import time

from laya.mcp.scope import enabled_tool_names

TOKEN_PREFIX = "lyat_turn_"

# token -> (allowed tool names, monotonic expiry)
_tokens: dict[str, tuple[frozenset[str], float]] = {}


def _prune(now: float) -> None:
    for tok, (_, expires) in list(_tokens.items()):
        if expires <= now:
            _tokens.pop(tok, None)


def mint(tool_names: set[str] | frozenset[str], ttl_seconds: float) -> str:
    """Mint a token whose MCP session may list and call only ``tool_names``.

    Names outside the read scope are dropped, so a turn token can never reach a
    write or egress tool whatever the caller passes.
    """
    allowed = frozenset(tool_names) & frozenset(enabled_tool_names({"read": True}))
    now = time.monotonic()
    _prune(now)
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    _tokens[token] = (allowed, now + max(1.0, float(ttl_seconds)))
    return token


def revoke(token: str) -> None:
    _tokens.pop(token, None)


def lookup(presented: str | None) -> frozenset[str] | None:
    """Return the tool allowlist of a live turn token, or None if it is not one."""
    if not presented or not presented.startswith(TOKEN_PREFIX):
        return None
    now = time.monotonic()
    _prune(now)
    for tok, (allowed, _) in _tokens.items():
        if hmac.compare_digest(tok, presented):
            return allowed
    return None


def lookup_header(auth_header: str | None) -> frozenset[str] | None:
    """``lookup`` for a raw ``Authorization`` header value."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    return lookup(auth_header[7:].strip())
