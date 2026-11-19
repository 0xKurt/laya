# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Headless `claude -p` turns and `gh` calls for the Claude Code connector.

A collector turn may only use the read tools it is given: `--allowedTools`
lists them, `--tools ""` removes every built-in tool, `--permission-mode
dontAsk` denies whatever is not pre-approved, and `--disallowedTools` names
every other discovered MCP tool. The explicit deny list matters because the
user's own Claude Code settings may allow a write tool (e.g. a Slack send)
globally, and a deny rule wins over any allow rule.

Every turn is logged to the audit log as model `agent/claude_code/<model>`,
so the agent usage budget (pipeline/agent_budget.py) counts it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from typing import Any

import structlog

from laya.config import get_agent_binary

log = structlog.get_logger()

DEFAULT_TIMEOUT = 600.0

# Cache reads cost a tenth of fresh input tokens.
_CACHE_READ_WEIGHT = 0.1


class ClaudeCodeError(RuntimeError):
    """A `claude -p` turn or a `gh` call failed."""


def weighted_input_tokens(usage: dict[str, Any]) -> int:
    return int(
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + _CACHE_READ_WEIGHT * (usage.get("cache_read_input_tokens") or 0)
    )


def build_args(
    binary: str,
    prompt: str,
    *,
    tools: list[str],
    model: str,
    schema: dict | None,
    deny: list[str] | None = None,
) -> list[str]:
    allowed = sorted(set(tools))
    denied = sorted(set(deny or []) - set(allowed))
    args = [
        binary, "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--allowedTools", ",".join(allowed),
    ]
    if denied:
        args += ["--disallowedTools", ",".join(denied)]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema)]
    return args


async def _run_process(args: list[str], timeout: float) -> list[str]:
    """Run a process in an empty temp dir and return its stdout lines."""
    from laya.agents.subprocess_helper import AgentProcess

    tmpdir = tempfile.mkdtemp(prefix="laya-claude-code-")
    proc = AgentProcess()
    try:
        await proc.spawn(args=args, cwd=tmpdir, env=None)

        async def _read() -> list[str]:
            lines: list[str] = []
            async for line in proc.read_lines(idle_timeout=timeout):
                lines.append(line)
            await proc.wait()
            return lines

        try:
            return await asyncio.wait_for(_read(), timeout=timeout)
        except asyncio.TimeoutError:
            await proc.terminate()
            raise ClaudeCodeError(f"claude timed out after {int(timeout)}s")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _run_turn(
    step: str,
    prompt: str,
    *,
    tools: list[str],
    model: str,
    schema: dict | None,
    timeout: float,
    deny: list[str] | None,
) -> dict[str, Any]:
    from laya.llm.client import log_to_audit

    args = build_args(
        get_agent_binary("claude_code"), prompt,
        tools=tools, model=model, schema=schema, deny=deny,
    )
    start = time.monotonic()
    try:
        lines = await _run_process(args, timeout)
        text = "\n".join(lines).strip()
        data = json.loads(text) if text else {}
    except (ClaudeCodeError, ValueError, OSError) as e:
        await log_to_audit(
            event_id=None, card_id=None, step=step, model=f"agent/claude_code/{model}",
            input_tokens=0, output_tokens=0,
            latency_ms=int((time.monotonic() - start) * 1000), success=False, error=str(e)[:500],
        )
        raise ClaudeCodeError(str(e)) from e

    usage = data.get("usage") or {}
    ok = not data.get("is_error") and (schema is None or data.get("structured_output") is not None)
    await log_to_audit(
        event_id=None, card_id=None, step=step, model=f"agent/claude_code/{model}",
        input_tokens=weighted_input_tokens(usage),
        output_tokens=usage.get("output_tokens") or 0,
        latency_ms=int((time.monotonic() - start) * 1000), success=ok,
        error=None if ok else str(data.get("result"))[:500],
        metadata={"cost_usd": data.get("total_cost_usd")} if data.get("total_cost_usd") else None,
    )
    if not ok:
        raise ClaudeCodeError(f"{step} failed: {str(data.get('result') or data)[:300]}")
    return data


async def run_collect(
    platform: str,
    prompt: str,
    schema: dict,
    tools: list[str],
    model: str = "haiku",
    timeout: float = DEFAULT_TIMEOUT,
    deny: list[str] | None = None,
) -> dict[str, Any]:
    """One read-only collector turn; returns the schema-validated structured output."""
    if not tools:
        raise ClaudeCodeError(f"no read tools discovered for {platform}")
    data = await _run_turn(
        f"claude_code_{platform}", prompt,
        tools=tools, model=model, schema=schema, timeout=timeout, deny=deny,
    )
    return data["structured_output"]


async def run_send(
    platform: str,
    prompt: str,
    tool: str,
    model: str = "haiku",
    timeout: float = DEFAULT_TIMEOUT,
    schema: dict | None = None,
    deny: list[str] | None = None,
) -> dict[str, Any]:
    """One send turn with exactly one send tool allowed. Returns the raw result
    (with `structured_output` when a schema is given)."""
    return await _run_turn(
        f"claude_code_{platform}_send", prompt,
        tools=[tool], model=model, schema=schema, timeout=timeout, deny=deny,
    )


async def read_init(timeout: float = 60.0) -> dict[str, Any]:
    """The `system/init` message of a headless session: its MCP servers and tools.

    The CLI prints this line before it calls the model, so the process is
    stopped right after it and the listing costs no tokens.
    """
    from laya.agents.subprocess_helper import AgentProcess

    args = [
        get_agent_binary("claude_code"), "-p", "reply ok",
        "--output-format", "stream-json", "--verbose",
        "--model", "haiku", "--max-turns", "1",
        "--tools", "", "--permission-mode", "dontAsk", "--no-session-persistence",
    ]
    tmpdir = tempfile.mkdtemp(prefix="laya-claude-code-")
    proc = AgentProcess()
    try:
        await proc.spawn(args=args, cwd=tmpdir, env=None)

        async def _first_init() -> dict[str, Any]:
            async for line in proc.read_lines(idle_timeout=timeout):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init":
                    return msg
            raise ClaudeCodeError(
                f"claude printed no init message: {proc.stderr_output[-300:]}"
            )

        try:
            return await asyncio.wait_for(_first_init(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ClaudeCodeError(f"claude did not start within {int(timeout)}s")
    finally:
        await proc.terminate()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def cli_version(binary: str | None = None) -> str | None:
    binary = binary or get_agent_binary("claude_code")
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode(errors="replace").strip() or None


async def run_gh(*args: str, timeout: float = 120.0) -> Any:
    """Run `gh <args>` and return its parsed JSON output (None for empty output)."""
    from laya.config import _augmented_path

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": _augmented_path(), "GH_PROMPT_DISABLED": "1"},
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError as e:
        raise ClaudeCodeError("gh is not installed") from e
    except asyncio.TimeoutError as e:
        raise ClaudeCodeError(f"gh {' '.join(args[:2])} timed out") from e
    if proc.returncode != 0:
        raise ClaudeCodeError(f"gh {' '.join(args[:2])} failed: {err.decode(errors='replace')[:300]}")
    text = out.decode(errors="replace").strip()
    return json.loads(text) if text else None
