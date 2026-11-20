# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Installed-CLI-agent inference backend — a parallel to the LiteLLM path.

Some users have an enterprise/subscription coding agent installed (Claude Code on
a Teams plan, ChatGPT/Codex, Gemini CLI, Pi) but no model API key and no spare VRAM
for local models. This backend drives those agents *as the LLM* — non-interactively,
with structured output — so Laya's pipeline runs on the agent's own quota instead of
an API key or local VRAM.

It is invoked from ``llm_call`` (``llm/client.py``) when the resolved model string is
``agent/<agent_id>/<model_string>``. It returns an :class:`AgentResult`; client.py
maps it into the same ``LLMResponse`` and keeps the shared audit-log + budget side
effects, so the contract is identical to the LiteLLM path.

Capability tiers (validated against the real CLIs):
  - **native** (Claude Code, Codex): Claude Code's ``--json-schema`` forces schema-valid
    output, returned pre-parsed in the ``structured_output`` field — same tool-based
    enforcement the Anthropic API uses. Codex's ``--output-schema`` constrains the final
    message with OpenAI structured outputs (strict mode), so the schema is converted to
    the strict form first (:func:`strict_schema`) and the reply parsed from the message.
  - **best_effort** (Gemini / Pi): no schema flag, so we inject the schema as
    a text instruction (the same one client.py uses for local providers), parse the
    JSON out of the reply, validate it, and retry on failure.

Streaming chat + Coherence (Claude Code only): :func:`agent_llm_stream` runs one
``claude -p`` turn with partial messages and yields Laya ``StreamEvent``s. When the
caller offers tools, the agent runs the tool loop itself against Laya's own MCP
endpoint, with a short-lived token limited to the read tools the caller offered
(:mod:`laya.mcp.turn_tokens`); tool use is reported as ``tool_status`` events and
the turn ends with no tool calls left for the caller to execute.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from laya.agents.subprocess_helper import AgentProcess
from laya.config import detect_agent_paths, get_agent_binary

if TYPE_CHECKING:
    from laya.llm.client import LLMResponse, StreamEvent

log = structlog.get_logger()

# Per-agent structured-output capability. "native" agents enforce the schema for us;
# "best_effort" agents get the schema as a prompt instruction + validate/retry.
AGENT_TIERS: dict[str, str] = {
    "claude_code": "native",
    "codex_cli": "native",
    "gemini_cli": "best_effort",
    "pi_cli": "best_effort",
}

# Built-in tools we deny so an inference call can never touch the filesystem, run a
# command, or hit the network — we want a pure completion, not an agent session. Each
# agent also runs in an ephemeral empty cwd (see _run_agent) so it has nothing to act on.
_CLAUDE_BUILTIN_TOOLS = (
    "Bash Edit Write Read WebFetch WebSearch Glob Grep Task TodoWrite NotebookEdit"
)

# Same schema-as-text instruction client.py appends for providers without native
# structured output — kept verbatim so best-effort agents see the identical contract.
_SCHEMA_INSTRUCTION = (
    "\n\nYou MUST respond with valid JSON matching this exact schema. "
    "Output ONLY the JSON object, no other text.\n\nSchema:\n{schema_text}"
)

# Transport-layer harness neutralizer. CLI agents ship a coding-assistant system prompt
# that, on a terse free-text task, makes the model ask clarifying questions or offer to
# take actions instead of just answering (schema calls are immune — the forced JSON tool
# can't be a question; this only bites no-schema stages like the briefing). No CLI flag
# disables that, so we prepend this directive ahead of the stage's own system prompt. It
# is NOT a stage prompt and never edits one — the stage prompt (single source of truth,
# shared with the LiteLLM path) follows it unchanged. Its only effect is to bring the
# agent's behavior in line with the API path (answer directly), so output stays faithful.
_NONINTERACTIVE_DIRECTIVE = (
    "You are operating as a non-interactive inference engine for automated text "
    "processing. Produce the requested output directly and completely in this single "
    "response. Do not ask clarifying questions, request more context, propose next "
    "steps, or offer to perform actions — return only the requested output."
)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", flags=re.DOTALL)

_semaphore: asyncio.Semaphore | None = None


@dataclass
class AgentResult:
    """What an agent inference call produced — mapped to LLMResponse by client.py."""

    content: str
    parsed: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    finish_reason: str = "stop"
    truncated: bool = False
    effective_model: str = ""
    cost_usd: float | None = None
    # Native usage-limit signal (Claude Code's rate_limit_info), for agent budgeting.
    rate_limit_info: dict | None = None
    # The Claude account the call ran on (claude_code/accounts.py); None for other agents.
    account_id: str | None = None


# ── helpers ──────────────────────────────────────────────────────────────


def is_agent_model(model: str | None) -> bool:
    """True if a resolved model string targets the agent backend."""
    return bool(model) and model.startswith("agent/")


def parse_agent_model_id(model_id: str) -> tuple[str, str | None]:
    """Split ``agent/<agent_id>/<model_string>`` → (agent_id, model_string|None).

    Splits on the first two slashes only: the agent id is a known enum (never contains
    a slash), and the remainder is the user-typed model string verbatim — so slugs with
    their own slashes (e.g. ``lmstudio/qwen3.6-35b-a3b``) survive intact. A missing third
    segment means "use the agent's own default model" (no --model flag).
    """
    parts = model_id.split("/", 2)
    if len(parts) < 2 or parts[0] != "agent":
        raise ValueError(f"Not an agent model id: {model_id!r}")
    agent_id = parts[1]
    model_string = parts[2] if len(parts) == 3 and parts[2].strip() else None
    return agent_id, model_string


def capabilities() -> list[dict[str, Any]]:
    """Per-agent availability + tier for the Settings UI agent picker."""
    detected = detect_agent_paths()
    out: list[dict[str, Any]] = []
    for agent_id, tier in AGENT_TIERS.items():
        path = detected.get(agent_id, "")
        out.append({
            "agent_id": agent_id,
            "available": bool(path),
            "path": path,
            "tier": tier,
        })
    return out


def _get_semaphore() -> asyncio.Semaphore:
    """Bound concurrent agent processes — each spawns a process and runs 5–12s."""
    global _semaphore
    if _semaphore is None:
        from laya.config import load_settings

        n = int(load_settings().get("agent_backend_concurrency", 3) or 3)
        _semaphore = asyncio.Semaphore(max(1, n))
    return _semaphore


def _strip_think_blocks(text: str) -> str:
    if "<think>" not in text:
        return text
    result = _THINK_BLOCK_RE.sub("", text).strip()
    if result.startswith("<think>"):
        result = result[len("<think>"):].strip()
    return result


def _split_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Collapse OpenAI-format messages into (system_text, user_prompt).

    Pipeline stages send a system + a single user message; we also tolerate multi-turn
    by labelling roles. Content may be a string or a content-block list (post-caching);
    we flatten either way.
    """
    def _text(content: Any) -> str:
        if isinstance(content, list):
            return "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        return str(content or "")

    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for m in messages:
        role = m.get("role")
        text = _text(m.get("content"))
        if role == "system":
            system_parts.append(text)
        else:
            turns.append((role or "user", text))

    system_text = "\n\n".join(p for p in system_parts if p)
    if len(turns) == 1:
        user_prompt = turns[0][1]
    else:
        user_prompt = "\n\n".join(f"[{r}]\n{t}" for r, t in turns)
    return system_text, user_prompt


def _extract_json(text: str) -> dict | None:
    """Pull the first complete JSON object out of an agent reply (best-effort path).

    Mirrors client.py: strip <think> blocks and markdown fences, then parse. Uses
    raw_decode from the first ``{`` so trailing prose doesn't break parsing.
    """
    if not text:
        return None
    stripped = _strip_think_blocks(text).strip()
    if stripped.startswith("```"):
        nl = stripped.find("\n")
        if nl != -1:
            stripped = stripped[nl + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    start = stripped.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(stripped[start:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _validate_against_schema(parsed: dict, response_schema: dict) -> tuple[bool, str]:
    """Validate parsed JSON against the (inner) JSON schema.

    Uses jsonschema when importable (it ships transitively via litellm); otherwise
    falls back to a top-level required-keys check, which catches the common failure
    modes (truncation, missing fields). Returns (ok, error_message).
    """
    schema = response_schema.get("schema", response_schema)
    try:
        import jsonschema

        jsonschema.validate(instance=parsed, schema=schema)
        return True, ""
    except ImportError:
        required = schema.get("required", []) if isinstance(schema, dict) else []
        missing = [k for k in required if k not in parsed]
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"
        return True, ""
    except Exception as e:  # jsonschema.ValidationError
        return False, str(e).splitlines()[0] if str(e) else "schema validation failed"


# Keywords OpenAI structured outputs (strict mode) does not accept; they are dropped
# from the schema given to Codex. The full schema still validates the reply.
_STRICT_DROP_KEYS = frozenset({
    "default", "examples", "minLength", "maxLength", "uniqueItems", "minProperties",
    "maxProperties", "patternProperties", "$schema", "format", "pattern",
})


class _NotStrict(ValueError):
    """The schema has a part strict mode cannot express (e.g. a free-form object)."""


def _strict_node(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict_node(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strict_node(v) for k, v in node.items() if k not in _STRICT_DROP_KEYS}
    if "oneOf" in out:
        out["anyOf"] = out.pop("oneOf")
    if "allOf" in out:
        raise _NotStrict("allOf")
    types = out.get("type")
    is_object = types == "object" or (isinstance(types, list) and "object" in types) or "properties" in out
    if is_object:
        props = out.get("properties")
        if not isinstance(props, dict) or not props:
            raise _NotStrict("object without properties")
        required = set(node.get("required") or [])
        for key, sub in list(props.items()):
            if key not in required:
                props[key] = _nullable(sub)
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _nullable(sub: Any) -> Any:
    """``sub`` that also accepts null (how strict mode expresses an optional field)."""
    if not isinstance(sub, dict):
        return sub
    sub = dict(sub)
    t = sub.get("type")
    if isinstance(t, str):
        if t != "null":
            sub["type"] = [t, "null"]
        if "enum" in sub and None not in sub["enum"]:
            sub["enum"] = [*sub["enum"], None]
        return sub
    if isinstance(t, list):
        if "null" not in t:
            sub["type"] = [*t, "null"]
        if "enum" in sub and None not in sub["enum"]:
            sub["enum"] = [*sub["enum"], None]
        return sub
    if "anyOf" in sub:
        if not any(isinstance(a, dict) and a.get("type") == "null" for a in sub["anyOf"]):
            sub["anyOf"] = [*sub["anyOf"], {"type": "null"}]
        return sub
    return {"anyOf": [sub, {"type": "null"}]}


def strict_schema(schema: dict) -> dict | None:
    """``schema`` in the form OpenAI structured outputs (strict) accepts, or None.

    Every object lists all its properties as required and forbids others; a
    property that was optional accepts null instead. None when the schema cannot
    be expressed that way (not an object at the top, a free-form object, allOf).
    """
    if not isinstance(schema, dict):
        return None
    try:
        out = _strict_node(schema)
    except _NotStrict:
        return None
    if out.get("type") != "object" and "properties" not in out:
        return None
    return out


def relax_optional(parsed: Any, schema: Any) -> Any:
    """Drop the null values strict mode produced for properties ``schema`` does not
    require, so the reply validates against the original schema."""
    if not isinstance(schema, dict):
        return parsed
    if isinstance(parsed, dict) and isinstance(schema.get("properties"), dict):
        required = set(schema.get("required") or [])
        out = {}
        for key, value in parsed.items():
            sub = schema["properties"].get(key)
            if value is None and key not in required and key in schema["properties"]:
                continue
            out[key] = relax_optional(value, sub) if sub is not None else value
        return out
    if isinstance(parsed, list) and isinstance(schema.get("items"), dict):
        return [relax_optional(v, schema["items"]) for v in parsed]
    return parsed


# Codex refused the output schema itself (not the reply): the call is retried with
# the schema as a prompt instruction.
_SCHEMA_REJECTED_RE = re.compile(
    r"invalid_json_schema|invalid schema|output[_ -]schema|response_format|text\.format",
    re.IGNORECASE,
)
# Set once Codex refused an output schema; later calls use the prompt instruction.
_codex_schema_unsupported = False


# ── per-agent process runner ─────────────────────────────────────────────


def _build_args(
    agent_id: str,
    binary: str,
    system_text: str,
    user_prompt: str,
    model_string: str | None,
    response_schema: dict | None,
    native: bool,
    workdir: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build the argv + env for one agent invocation. env overrides only.

    ``workdir`` is the call's ephemeral working directory; Codex's output schema
    file is written there.

    NON-INTERACTIVE GUARANTEE (text processing must never block on a human): every agent
    runs in one-shot headless mode with stdin closed (AgentProcess spawns with
    stdin=DEVNULL), so a clarifying question can only ever be emitted as output text — it
    can never pause the turn waiting for a reply. On top of that we disable tools and any
    "plan/act"/approval gating per-agent below, so there's nothing to approve and no
    workspace-trust prompt to satisfy. None of this touches the stage prompts (single
    source of truth shared with the LiteLLM path) — it's all calling-convention.
    """
    env: dict[str, str] = {}

    if agent_id == "claude_code":
        # -p = one-shot print mode (exits, never waits). All built-in tools denied = pure
        # inference, nothing to request approval for. NO --mcp-config (keeps the per-call
        # system-prompt tax down and avoids loading Laya's tools).
        #
        # --safe-mode skips the user's CLAUDE.md, skills, plugins, hooks and MCP servers
        # while keeping the subscription login; --strict-mcp-config (with no --mcp-config)
        # and an empty --setting-sources keep MCP servers and user/project settings out
        # as well. Without them every call carries the user's whole Claude Code setup in
        # its prompt and runs their hooks.
        #
        # permission-mode=default (NOT plan): in plan mode Claude wraps the answer in
        # "I'm in plan mode … once plan mode is exited I can help you …" preamble — a
        # plan-and-wait framing we must avoid. In headless -p with tools denied, default
        # mode cannot prompt (no TTY) and cannot act, so it just answers directly.
        #
        # stream-json (+ --verbose, required) instead of plain json so we also capture the
        # rate_limit_event (window reset + status) for agent usage-budgeting; the final
        # `result` event still carries structured_output + usage + total_cost_usd.
        args = [binary, "-p", user_prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "default", "--disallowedTools", _CLAUDE_BUILTIN_TOOLS,
                "--safe-mode", "--strict-mcp-config", "--setting-sources", ""]
        if system_text:
            args += ["--append-system-prompt", system_text]
        if model_string:
            args += ["--model", model_string]
        if native and response_schema:
            schema = response_schema.get("schema", response_schema)
            args += ["--json-schema", json.dumps(schema)]
        return args, env

    if agent_id == "pi_cli":
        # -p = one-shot non-interactive; --no-tools = nothing to approve; --no-session =
        # don't litter the cwd. Pi's plan-mode extension is not enabled, so no plan/wait.
        args = [binary, "-p", "--mode", "json", "--no-tools", "--no-session"]
        if system_text:
            args += ["--system-prompt", system_text]
        if model_string:
            args += ["--model", model_string]
        args.append(user_prompt)
        return args, env

    if agent_id == "gemini_cli":
        # -p = one-shot headless; --approval-mode plan = read-only (no edits, and headless
        # so nothing to approve). GEMINI_CLI_TRUST_WORKSPACE skips the folder-trust prompt
        # that otherwise aborts headless runs in an untrusted dir.
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        prompt = f"{system_text}\n\n{user_prompt}" if system_text else user_prompt
        args = [binary, "-p", prompt, "-o", "json", "--approval-mode", "plan"]
        if model_string:
            args += ["-m", model_string]
        return args, env

    if agent_id == "codex_cli":
        # exec = the non-interactive automation mode: it runs to completion and never
        # asks for an approval (exec has no --ask-for-approval flag); --sandbox read-only
        # = pure inference. --skip-git-repo-check lets it run in the empty temp cwd,
        # --ephemeral keeps the call out of the user's session history, and
        # --ignore-user-config skips $CODEX_HOME/config.toml (the user's MCP servers,
        # profiles and instructions) while keeping the login — the analogue of
        # Claude Code's --safe-mode above.
        prompt = f"{system_text}\n\n{user_prompt}" if system_text else user_prompt
        args = [binary, "exec", "--json", "--sandbox", "read-only",
                "--skip-git-repo-check", "--ephemeral", "--ignore-user-config"]
        if model_string:
            args += ["--model", model_string]
        if native and response_schema:
            schema_path = os.path.join(workdir or tempfile.gettempdir(), "laya-output-schema.json")
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump(strict_schema(response_schema.get("schema", response_schema)), f)
            args += ["--output-schema", schema_path]
        args.append(prompt)
        return args, env

    raise ValueError(f"Unknown agent backend: {agent_id}")


# Cache reads cost a tenth of fresh input (API pricing, and roughly their weight on
# subscription usage limits). Every `claude -p` call re-reads the cached system prompt,
# so counting cache reads in full would make each call look several times its cost.
CACHE_READ_WEIGHT = 0.1


def weighted_input_tokens(usage: dict) -> int:
    """Input tokens of a Claude Code `result` usage block, cache reads weighted."""
    return int(
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0) * CACHE_READ_WEIGHT
    )


def _parse_output(agent_id: str, lines: list[str], native_schema: bool) -> dict[str, Any]:
    """Parse an agent's stdout into a normalized dict.

    Returns: content, parsed (native only), input_tokens, output_tokens,
    finish_reason, effective_model, cost_usd, error.
    """
    out: dict[str, Any] = {
        "content": "", "parsed": None, "input_tokens": 0, "output_tokens": 0,
        "finish_reason": "stop", "effective_model": "", "cost_usd": None,
        "rate_limit_info": None, "error": None,
    }
    buf = "\n".join(lines)

    if agent_id == "claude_code":
        # stream-json: scan for the final `result` event (the answer + usage) and the
        # `rate_limit_event` (usage-window state). Other event types are ignored.
        data: dict = {}
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "result":
                data = ev
            elif etype == "rate_limit_event":
                out["rate_limit_info"] = ev.get("rate_limit_info")
        if data.get("is_error"):
            out["error"] = str(data.get("result") or data.get("error") or "claude error")
            return out
        if native_schema:
            out["parsed"] = data.get("structured_output")
        out["content"] = data.get("result") or (
            json.dumps(out["parsed"]) if out["parsed"] is not None else ""
        )
        usage = data.get("usage") or {}
        out["input_tokens"] = weighted_input_tokens(usage)
        out["output_tokens"] = int(usage.get("output_tokens", 0))
        out["cost_usd"] = data.get("total_cost_usd")
        model_usage = data.get("modelUsage") or {}
        out["effective_model"] = next(iter(model_usage), "") or ""
        if data.get("stop_reason") == "max_tokens":
            out["finish_reason"] = "length"
        return out

    if agent_id == "gemini_cli":
        data = _extract_json(buf) or {}
        if data.get("error"):
            err = data["error"]
            out["error"] = err.get("message") if isinstance(err, dict) else str(err)
            return out
        out["content"] = str(data.get("response", ""))
        stats = data.get("stats") or {}
        out["input_tokens"], out["output_tokens"] = _gemini_tokens(stats)
        return out

    # JSONL event streams: Pi and Codex
    if agent_id == "pi_cli":
        return _parse_pi(lines, out)
    if agent_id == "codex_cli":
        return _parse_codex(lines, out, native_schema)

    return out


def _gemini_tokens(stats: dict) -> tuple[int, int]:
    """Best-effort token extraction from Gemini's nested stats blob."""
    def _find(d: Any, keys: tuple[str, ...]) -> int:
        if isinstance(d, dict):
            for k, v in d.items():
                if k in keys and isinstance(v, (int, float)):
                    return int(v)
                r = _find(v, keys)
                if r:
                    return r
        elif isinstance(d, list):
            for v in d:
                r = _find(v, keys)
                if r:
                    return r
        return 0
    return _find(stats, ("prompt", "promptTokenCount", "input")), _find(
        stats, ("candidates", "candidatesTokenCount", "output")
    )


def _parse_pi(lines: list[str], out: dict[str, Any]) -> dict[str, Any]:
    """Accumulate Pi's assistant text + usage from its JSON event stream."""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        msg = ev.get("message") or {}
        if etype in ("message_update", "message_end") and msg.get("role") == "assistant":
            content = msg.get("content") or []
            text = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
            if text:
                out["content"] = text
            usage = msg.get("usage") or {}
            if usage.get("input") or usage.get("output"):
                out["input_tokens"] = int(usage.get("input", 0))
                out["output_tokens"] = int(usage.get("output", 0))
                cost = usage.get("cost") or {}
                out["cost_usd"] = cost.get("total") if isinstance(cost, dict) else None
            if msg.get("model"):
                out["effective_model"] = str(msg["model"])
    return out


def _parse_codex(lines: list[str], out: dict[str, Any], native_schema: bool = False) -> dict[str, Any]:
    """Accumulate Codex's final agent_message + usage from its JSONL events.

    A turn fails with ``turn.failed`` (its ``error.message``); an ``error`` event
    alone fails it only when no ``turn.completed`` follows (Codex also reports
    retried stream errors this way). With an output schema the final message is
    the JSON reply.
    """
    error_event: str | None = None
    failed: str | None = None
    completed = False
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type", "")
        if etype.startswith("item."):
            item = ev.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message"):
                text = item.get("text") or item.get("message") or ""
                if text:
                    out["content"] = str(text)
        elif etype == "turn.completed":
            completed = True
            usage = ev.get("usage") or {}
            total_in = int(usage.get("input_tokens", usage.get("input", 0)) or 0)
            cached = int(usage.get("cached_input_tokens") or 0)
            # input_tokens includes the cached part; cache reads are weighted like
            # Claude Code's (see CACHE_READ_WEIGHT).
            out["input_tokens"] = int(total_in - cached + cached * CACHE_READ_WEIGHT)
            out["output_tokens"] = int(usage.get("output_tokens", usage.get("output", 0)) or 0)
        elif etype == "turn.failed":
            err = ev.get("error")
            failed = str((err.get("message") if isinstance(err, dict) else err) or "codex turn failed")
        elif etype == "error":
            error_event = str(ev.get("message") or ev.get("error") or "codex error")
    if failed:
        out["error"] = failed
    elif error_event and not completed:
        out["error"] = error_event
    if native_schema and out["content"] and not out["error"]:
        out["parsed"] = _extract_json(out["content"])
    return out


class AgentRateLimited(RuntimeError):
    """The login's usage limit (a Claude account, a Codex login) rejected the turn."""


async def _run_agent_routed(agent_id: str, *args: Any) -> dict[str, Any]:
    """``_run_agent`` on the login routing picks, for agents with logins
    (Claude Code accounts, Codex logins; see ``accounts.login_module``).

    A rejection by that login's usage limit is retried once on the next login
    of the same agent with room. The result carries the login in ``account_id``.
    """
    from laya.claude_code import accounts

    logins = accounts.login_module(agent_id)
    if logins is None:
        return await _run_agent(agent_id, *args)

    account_id, env = await logins.pick("inference")
    try:
        result = await _run_agent(agent_id, *args, account_id=account_id, account_env=env)
    except AgentRateLimited:
        nxt = await logins.failover(account_id)
        if nxt is None:
            raise
        account_id, env = nxt
        result = await _run_agent(agent_id, *args, account_id=account_id, account_env=env)
    result["account_id"] = account_id
    return result


async def _run_agent(
    agent_id: str,
    binary: str,
    system_text: str,
    user_prompt: str,
    model_string: str | None,
    response_schema: dict | None,
    native: bool,
    timeout: float,
    account_id: str | None = None,
    account_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn the agent once in an ephemeral empty cwd and return parsed output.

    ``account_env`` selects a Claude account (``CLAUDE_CONFIG_DIR``); the turn's
    rate-limit signal is recorded for ``account_id`` before any error is raised.
    A rejection by the account's usage limit raises :class:`AgentRateLimited`.
    """
    # Ephemeral, empty working dir: the agent has no repo to read or write, reinforcing
    # the tool/sandbox flags above. Cleaned up regardless of outcome.
    tmpdir = tempfile.mkdtemp(prefix="laya-agent-llm-")
    proc = AgentProcess()
    try:
        args, env = _build_args(
            agent_id, binary, system_text, user_prompt, model_string, response_schema, native,
            workdir=tmpdir,
        )
        if account_env:
            env = {**env, **account_env}
        await proc.spawn(args=args, cwd=tmpdir, env=env or None)

        async def _collect() -> list[str]:
            collected: list[str] = []
            async for line in proc.read_lines(idle_timeout=timeout):
                collected.append(line)
            await proc.wait()
            return collected

        try:
            lines = await asyncio.wait_for(_collect(), timeout=timeout)
        except asyncio.TimeoutError:
            await proc.terminate()
            raise TimeoutError(f"agent '{agent_id}' timed out after {timeout}s")
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Agent '{agent_id}' binary not found ({binary}). Is it installed?"
        ) from e
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    result = _parse_output(agent_id, lines, native_schema=native and bool(response_schema))
    if agent_id == "claude_code":
        from laya.claude_code import accounts

        rejected = accounts.is_rejection(result.get("rate_limit_info"), result.get("error"))
        await accounts.record(account_id, result.get("rate_limit_info"), rejected=rejected)
        if result.get("error") and rejected:
            raise AgentRateLimited(f"Agent '{agent_id}' error: {result['error']}")
    elif agent_id == "codex_cli":
        from laya.claude_code import codex_accounts

        # A usage limit arrives as the turn's error (JSONL), or on stderr when the
        # CLI failed before printing events.
        info = codex_accounts.limit_info(result.get("error")) or (
            None if result.get("content") else codex_accounts.limit_info(proc.stderr_output)
        )
        if info:
            result["rate_limit_info"] = info
            await codex_accounts.record(account_id, info)
            raise AgentRateLimited(f"Agent '{agent_id}' error: {info['message']}")
    if result.get("error"):
        stderr_tail = proc.stderr_output
        raise RuntimeError(
            f"Agent '{agent_id}' error: {result['error']}"
            + (f" | stderr: {stderr_tail[-500:]}" if stderr_tail else "")
        )
    return result


# ── public entry point ───────────────────────────────────────────────────


async def agent_llm_call(
    model_id: str,
    messages: list[dict[str, Any]],
    response_schema: dict | None = None,
    max_tokens: int = 65536,
    temperature: float = 0.0,
    num_retries: int = 3,
) -> AgentResult:
    """Run one inference through an installed CLI agent. Mirrors the llm_call contract.

    For native-schema agents (Claude Code) the schema is enforced by the CLI and the
    parsed object is returned directly. For best-effort agents the schema is injected as
    a prompt instruction, then the reply is parsed + validated + retried up to
    ``num_retries`` times before giving up (``parsed=None``, like the LiteLLM path).
    """
    global _codex_schema_unsupported
    agent_id, model_string = parse_agent_model_id(model_id)
    if agent_id not in AGENT_TIERS:
        raise ValueError(f"Unknown agent backend: {agent_id!r}")

    # Hard daily cap: raises AgentCallLimitReached (and pauses ingestion) once today's
    # agent calls reach the limit.
    from laya.pipeline.agent_budget import enforce_daily_call_limit

    await enforce_daily_call_limit()

    binary = get_agent_binary(agent_id)
    native = AGENT_TIERS[agent_id] == "native"
    if agent_id == "codex_cli" and response_schema and (
        _codex_schema_unsupported
        or strict_schema(response_schema.get("schema", response_schema)) is None
    ):
        native = False
    system_text, user_prompt = _split_messages(messages)

    def system_for(native_call: bool) -> str:
        # The non-interactive directive goes ahead of the (unchanged) stage system
        # prompt so the agent answers directly instead of asking clarifying questions.
        # Without a native schema flag the schema is given as text, the same way
        # client.py does for local providers without native structured output.
        text = (_NONINTERACTIVE_DIRECTIVE + ("\n\n" + system_text if system_text else "")).strip()
        if response_schema and not native_call:
            schema_text = json.dumps(response_schema.get("schema", response_schema), indent=2)
            text = (text + _SCHEMA_INSTRUCTION.format(schema_text=schema_text)).strip()
        return text

    base_system = system_for(native)

    from laya.pipeline.queue import get_model_timeout

    timeout = float(get_model_timeout())
    attempts = max(1, min(num_retries, 3)) if response_schema else 1
    last_content = ""
    last_usage = {"input_tokens": 0, "output_tokens": 0}
    last_extra: dict[str, Any] = {}
    retry_feedback = ""
    parsed = None

    async with _get_semaphore():
        attempt = 0
        while attempt < attempts:
            system_for_attempt = base_system
            if retry_feedback:
                system_for_attempt = (base_system + retry_feedback).strip()

            start = time.monotonic()
            try:
                result = await _run_agent_routed(
                    agent_id, binary, system_for_attempt, user_prompt, model_string,
                    response_schema, native, timeout,
                )
            except RuntimeError as e:
                if isinstance(e, AgentRateLimited) or not (
                    agent_id == "codex_cli" and native and _SCHEMA_REJECTED_RE.search(str(e))
                ):
                    raise
                # Codex refused the strict output schema itself: this and later calls
                # give the schema as a prompt instruction instead.
                _codex_schema_unsupported = True
                native = False
                base_system = system_for(False)
                log.warning("codex_output_schema_refused", error=str(e)[:300])
                continue
            attempt += 1
            log.info(
                "agent_llm_attempt",
                agent_id=agent_id, attempt=attempt, native=native,
                latency_ms=int((time.monotonic() - start) * 1000),
                output_tokens=result.get("output_tokens", 0),
            )
            last_content = result["content"]
            last_usage = {
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
            }
            last_extra = {
                "finish_reason": result["finish_reason"],
                "effective_model": result["effective_model"] or (model_string or agent_id),
                "cost_usd": result["cost_usd"],
                "rate_limit_info": result.get("rate_limit_info"),
                "account_id": result.get("account_id"),
            }

            if not response_schema:
                parsed = None
                break

            # Native agents return the object directly (Codex: strict-mode nulls of
            # optional fields dropped again).
            if native:
                parsed = result["parsed"]
                if agent_id == "codex_cli":
                    parsed = relax_optional(parsed, response_schema.get("schema", response_schema))
            else:
                parsed = _extract_json(result["content"])
            if parsed is not None:
                ok, err = _validate_against_schema(parsed, response_schema)
                if ok:
                    break
                retry_feedback = (
                    f"\n\nYour previous response did not match the schema: {err}. "
                    f"Return ONLY a corrected JSON object matching the schema exactly."
                )
            else:
                retry_feedback = (
                    "\n\nYour previous response was not valid JSON. "
                    "Return ONLY a single JSON object matching the schema, no other text."
                )
            log.warning(
                "agent_llm_invalid_output",
                agent_id=agent_id, attempt=attempt, will_retry=attempt < attempts,
            )

    return AgentResult(
        content=last_content,
        parsed=parsed if response_schema else None,
        input_tokens=last_usage["input_tokens"],
        output_tokens=last_usage["output_tokens"],
        finish_reason=last_extra.get("finish_reason", "stop"),
        truncated=last_extra.get("finish_reason") == "length",
        effective_model=last_extra.get("effective_model", ""),
        cost_usd=last_extra.get("cost_usd"),
        rate_limit_info=last_extra.get("rate_limit_info"),
        account_id=last_extra.get("account_id"),
    )


# ── streaming turn (chat / Coherence) ────────────────────────────────────

# Name of Laya's own MCP server in a turn's --mcp-config; Claude Code exposes its tools
# as ``mcp__laya__<tool>``.
LAYA_MCP_SERVER_NAME = "laya"
_LAYA_TOOL_PREFIX = f"mcp__{LAYA_MCP_SERVER_NAME}__"

# Directive ahead of a streaming turn's own system prompt (chat / Coherence). The chat
# prompt is conversational, so this directive keeps the reply addressed to the user
# instead of the non-interactive "inference engine" framing used for pipeline stages.
_STREAM_DIRECTIVE = (
    "You are the language model behind Laya, a desktop app, answering inside its chat "
    "or narrative view. Reply to the user directly, following the instructions below. "
    "You have no filesystem, shell or web access."
)
_STREAM_TOOLS_DIRECTIVE = (
    " Laya's tools are available to you as MCP tools named `mcp__laya__<tool>` (for "
    "example `mcp__laya__search_cards` for the tool the instructions call "
    "`search_cards`). Only Laya's read tools are available in this session: you cannot "
    "change cards, settings or rules, or prepare outbound messages here; when the user "
    "asks for that, say it must be done in Laya's UI."
)


def laya_read_tool_names(tools: list[dict] | None) -> set[str]:
    """Names of the read-scope tools among OpenAI-format tool definitions."""
    if not tools:
        return set()
    from laya.llm.tools.definitions import read_tool_names

    offered = {
        (t.get("function") or {}).get("name")
        for t in tools
        if isinstance(t, dict)
    }
    return {n for n in offered if n} & read_tool_names()


def build_turn_mcp_config(token: str, space_id: str | None) -> dict[str, Any]:
    """MCP config for one turn: only Laya's Streamable HTTP endpoint, with a turn token."""
    from urllib.parse import quote

    from laya.config import ENGINE_HOST, ENGINE_PORT

    url = f"http://{ENGINE_HOST}:{ENGINE_PORT}/mcp/"
    if space_id:
        url += f"?space_id={quote(space_id, safe='')}"
    return {
        "mcpServers": {
            LAYA_MCP_SERVER_NAME: {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def _write_private_json(data: dict[str, Any]) -> str:
    """Write ``data`` to an owner-only temp file and return its path.

    The MCP config carries a bearer token; a file argument keeps it out of the
    process list, where an inline ``--mcp-config '<json>'`` would show it.
    """
    fd, path = tempfile.mkstemp(prefix="laya_agent_mcp_", suffix=".json")
    try:
        os.write(fd, json.dumps(data).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _claude_stream_args(
    binary: str,
    system_text: str,
    user_prompt: str,
    model_string: str | None,
    mcp_config_path: str | None = None,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    """argv for a streaming turn: the inference flags plus partial messages, and for a
    tool turn Laya's MCP server with an allowlist of its tools."""
    args, _ = _build_args(
        "claude_code", binary, system_text, user_prompt, model_string, None, True
    )
    args.append("--include-partial-messages")
    if mcp_config_path:
        # --safe-mode also disables servers given through --mcp-config, so a tool turn
        # runs without it. --strict-mcp-config (already set) keeps every other MCP
        # server out, the empty --setting-sources keeps user/project settings and hooks
        # out, and --tools "" removes all built-in tools.
        args.remove("--safe-mode")
        args += ["--tools", "", "--mcp-config", mcp_config_path]
        for name in allowed_tools or []:
            args += ["--allowedTools", f"{_LAYA_TOOL_PREFIX}{name}"]
    return args


def _short_tool_name(name: str) -> str:
    return name[len(_LAYA_TOOL_PREFIX):] if name.startswith(_LAYA_TOOL_PREFIX) else name


def _tool_result_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    return str(content or "")


class ClaudeStreamParser:
    """Turn ``claude -p --output-format stream-json --include-partial-messages`` lines
    into stream items.

    ``feed`` returns a list of items:
      - ``("text", str)``: a piece of reply text;
      - ``("tool", "calling", id, name, arguments)``: the agent called a tool;
      - ``("tool", "done", id, name, arguments, result_text, is_error)``: its result.

    Text comes from ``text_delta`` partial events. A message whose text arrived only as
    a complete ``assistant`` event (no partial events) is emitted from that event, so
    no text is lost or doubled. Text of consecutive assistant messages (before and after
    a tool call) is separated by a blank line. Thinking blocks are not emitted.
    """

    def __init__(self) -> None:
        self.result: dict | None = None
        self.rate_limit_info: dict | None = None
        self._current_msg: str | None = None
        self._streamed_msgs: set[str] = set()
        self._last_text_msg: str | None = None
        self._pending_tools: dict[str, tuple[str, dict]] = {}
        self.tools_used: list[str] = []

    def _text(self, msg_id: str | None, text: str) -> tuple:
        if self._last_text_msg is not None and msg_id != self._last_text_msg:
            text = "\n\n" + text
        self._last_text_msg = msg_id
        return ("text", text)

    def feed(self, line: str) -> list[tuple]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        etype = ev.get("type")
        out: list[tuple] = []

        if etype == "stream_event":
            if ev.get("parent_tool_use_id"):
                return []
            inner = ev.get("event") or {}
            itype = inner.get("type")
            if itype == "message_start":
                self._current_msg = (inner.get("message") or {}).get("id")
            elif itype == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    if self._current_msg:
                        self._streamed_msgs.add(self._current_msg)
                    out.append(self._text(self._current_msg, delta["text"]))

        elif etype == "assistant":
            if ev.get("parent_tool_use_id"):
                return []
            msg = ev.get("message") or {}
            msg_id = msg.get("id")
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text") and msg_id not in self._streamed_msgs:
                    out.append(self._text(msg_id, block["text"]))
                elif btype == "tool_use" and block.get("id"):
                    tid = block["id"]
                    if tid in self._pending_tools:
                        continue
                    name = _short_tool_name(str(block.get("name") or ""))
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    self._pending_tools[tid] = (name, args)
                    self.tools_used.append(name)
                    out.append(("tool", "calling", tid, name, args))

        elif etype == "user":
            if ev.get("parent_tool_use_id"):
                return []
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                name, args = self._pending_tools.pop(tid, ("", {}))
                if not name:
                    continue
                out.append((
                    "tool", "done", tid, name, args,
                    _tool_result_text(block.get("content")), bool(block.get("is_error")),
                ))

        elif etype == "rate_limit_event":
            self.rate_limit_info = ev.get("rate_limit_info")

        elif etype == "result":
            self.result = ev

        return out


async def _account_stream_turn(
    model_id: str,
    step: str,
    *,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    success: bool,
    error: str | None = None,
    cost_usd: float | None = None,
    tools_used: list[str] | None = None,
    account_id: str | None = None,
    event_id: str | None = None,
    card_id: str | None = None,
) -> None:
    """Audit + budget side effects of one streaming turn, matching llm_call's agent path:
    the audit row (which also counts toward the daily agent call cap), the $ budget
    check and the window-budget evaluation. The turn itself records its native
    rate-limit signal for the account it ran on."""
    from laya.llm.client import log_to_audit

    meta: dict[str, Any] = {"backend": "agent", "agent_model": model_id, "streaming": True}
    if cost_usd is not None:
        meta["cost_usd"] = cost_usd
    if tools_used:
        meta["agent_tools"] = tools_used
    if account_id:
        meta["claude_account"] = account_id
    await log_to_audit(
        event_id=event_id,
        card_id=card_id,
        step=step,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        success=success,
        error=error,
        metadata=meta,
    )
    if not success:
        return
    try:
        from laya.pipeline.budget import check_budget
        from laya.tasks import create_task as create_tracked_task

        create_tracked_task(check_budget())
    except Exception:
        pass
    try:
        from laya.pipeline.agent_budget import evaluate_agent_budget
        from laya.tasks import create_task as create_tracked_task

        create_tracked_task(evaluate_agent_budget())
    except Exception:
        pass


async def agent_llm_stream(
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    step: str = "chat",
    tools: list[dict] | None = None,
    space_id: str | None = None,
    event_id: str | None = None,
    card_id: str | None = None,
) -> AsyncGenerator["StreamEvent", None]:
    """Run one streaming turn on an agent model and yield Laya ``StreamEvent``s.

    Yields ``chunk`` events for reply text, ``tool_status`` events (``tool_status`` =
    "calling" / "done", the call in ``tool_calls``, the result text in ``content`` on
    "done") when the agent uses one of Laya's tools, then one ``done`` event with
    usage, or one ``error`` event. It never yields ``tool_calls``: the agent runs the
    tool loop itself, so there is nothing left for the caller to execute.

    ``tools`` are the OpenAI-format definitions the caller would have offered; the turn
    gets the read-scope ones through Laya's MCP endpoint (see the module docstring).
    """
    from laya.llm.client import StreamEvent, ToolCall
    from laya.pipeline.agent_budget import AgentBudgetRefused, enforce_daily_call_limit

    agent_id, model_string = parse_agent_model_id(model_id)
    if agent_id != "claude_code":
        yield StreamEvent(
            type="error",
            model=model_id,
            content=(
                "Streaming chat and Coherence on an agent backend need Claude Code. "
                "Choose Claude Code or an API/local model for the chat and trace roles."
            ),
        )
        return

    try:
        await enforce_daily_call_limit()
    except AgentBudgetRefused as e:
        # The turn never ran, so there is no call to audit.
        yield StreamEvent(type="error", content=str(e), model=model_id)
        return

    from laya.pipeline.queue import get_model_timeout

    idle_timeout = float(get_model_timeout())
    # A tool turn runs several model rounds; the idle timeout bounds each silence and
    # this deadline bounds the whole turn.
    deadline_s = idle_timeout * 3
    binary = get_agent_binary(agent_id)
    system_text, user_prompt = _split_messages(messages)

    allowed = sorted(laya_read_tool_names(tools))
    directive = _STREAM_DIRECTIVE + (_STREAM_TOOLS_DIRECTIVE if allowed else "")
    full_system = (directive + ("\n\n" + system_text if system_text else "")).strip()

    token: str | None = None
    config_path: str | None = None
    account_id: str | None = None
    tmpdir = tempfile.mkdtemp(prefix="laya-agent-stream-")
    proc = AgentProcess()
    finished = False
    start = time.monotonic()
    parser = ClaudeStreamParser()
    output_text = ""
    try:
        if allowed:
            from laya.mcp import turn_tokens

            token = turn_tokens.mint(set(allowed), ttl_seconds=deadline_s + 60)
            config_path = _write_private_json(build_turn_mcp_config(token, space_id))
        args = _claude_stream_args(
            binary, full_system, user_prompt, model_string, config_path, allowed,
        )

        from laya.claude_code import accounts

        account_id, account_env = await accounts.pick("chat")
        retried = False
        while True:
            yielded = False
            async with _get_semaphore():
                try:
                    await proc.spawn(args=args, cwd=tmpdir, env=account_env or None)
                except FileNotFoundError as e:
                    raise RuntimeError(
                        f"Agent '{agent_id}' binary not found ({binary}). Is it installed?"
                    ) from e
                async for line in proc.read_lines(idle_timeout=idle_timeout):
                    for item in parser.feed(line):
                        yielded = True
                        if item[0] == "text":
                            output_text += item[1]
                            yield StreamEvent(type="chunk", content=item[1], model=model_id)
                        else:
                            _, status, tid, name, targs = item[:5]
                            yield StreamEvent(
                                type="tool_status",
                                tool_status=status,
                                tool_calls=[ToolCall(id=tid, name=name, arguments=targs)],
                                content=item[5] if status == "done" else "",
                                model=model_id,
                            )
                    if time.monotonic() - start > deadline_s:
                        raise TimeoutError(f"agent '{agent_id}' turn exceeded {int(deadline_s)}s")
                await proc.wait()
                finished = True

            data = parser.result
            error_text = (
                str(data.get("result") or data.get("error") or "")
                if data and data.get("is_error") else None
            )
            rejected = accounts.is_rejection(parser.rate_limit_info, error_text)
            await accounts.record(account_id, parser.rate_limit_info, rejected=rejected)
            # A turn the account's usage limit rejected is retried once on the next
            # account, but only while nothing has been shown to the user.
            if rejected and not yielded and not retried:
                nxt = await accounts.failover(account_id)
                if nxt is not None:
                    account_id, account_env = nxt
                    retried = True
                    proc = AgentProcess()
                    finished = False
                    parser = ClaudeStreamParser()
                    continue
            break

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if data is None:
            stderr_tail = proc.stderr_output
            raise RuntimeError(
                f"Agent '{agent_id}' ended without a result"
                + (f" | stderr: {stderr_tail[-500:]}" if stderr_tail else "")
            )
        if data.get("is_error"):
            raise RuntimeError(
                f"Agent '{agent_id}' error: "
                f"{data.get('result') or data.get('error') or 'claude error'}"
            )
        if not output_text and data.get("result"):
            # No text arrived as events (e.g. an older CLI); emit the final answer whole.
            output_text = str(data["result"])
            yield StreamEvent(type="chunk", content=output_text, model=model_id)

        usage = data.get("usage") or {}
        input_tokens = weighted_input_tokens(usage)
        output_tokens = int(usage.get("output_tokens") or 0)
        finish_reason = "length" if data.get("stop_reason") == "max_tokens" else "stop"
        log.info(
            "agent_llm_stream_complete",
            agent_id=agent_id, step=step, latency_ms=elapsed_ms,
            output_tokens=output_tokens, tools=parser.tools_used,
        )
        yield StreamEvent(
            type="done",
            content=output_text,
            model=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            finish_reason=finish_reason,
        )
        await _account_stream_turn(
            model_id, step,
            input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=elapsed_ms,
            success=True, cost_usd=data.get("total_cost_usd"),
            tools_used=parser.tools_used, account_id=account_id,
            event_id=event_id, card_id=card_id,
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.error("agent_llm_stream_failed", agent_id=agent_id, step=step, error=str(e))
        yield StreamEvent(type="error", content=str(e), model=model_id, latency_ms=elapsed_ms)
        await _account_stream_turn(
            model_id, step, input_tokens=0, output_tokens=0, latency_ms=elapsed_ms,
            success=False, error=str(e), account_id=account_id,
            event_id=event_id, card_id=card_id,
        )
    finally:
        if not finished:
            await proc.terminate()
        if token:
            from laya.mcp import turn_tokens

            turn_tokens.revoke(token)
        if config_path:
            try:
                os.unlink(config_path)
            except OSError:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def agent_tool_turn(
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    step: str,
    tools: list[dict] | None,
    space_id: str | None = None,
    event_id: str | None = None,
    card_id: str | None = None,
) -> "LLMResponse":
    """Non-streaming ``llm_call(..., tools=...)`` on an agent model.

    Runs the same turn as :func:`agent_llm_stream` (the agent calls Laya's read tools
    itself) and returns the final text with no tool calls, so a caller's tool loop ends
    after this one call. Raises on error, like ``llm_call``.
    """
    from laya.llm.client import LLMResponse

    content = ""
    done = None
    async for ev in agent_llm_stream(
        model_id, messages, step=step, tools=tools, space_id=space_id,
        event_id=event_id, card_id=card_id,
    ):
        if ev.type == "error":
            raise RuntimeError(ev.content)
        if ev.type == "done":
            done = ev
        elif ev.type == "chunk":
            content += ev.content
    if done is None:
        raise RuntimeError(f"Agent turn on {model_id} ended without a result")
    return LLMResponse(
        content=done.content or content,
        model=model_id,
        input_tokens=done.input_tokens,
        output_tokens=done.output_tokens,
        latency_ms=done.latency_ms,
        finish_reason=done.finish_reason or "stop",
        truncated=done.finish_reason == "length",
        tool_calls=None,
    )
