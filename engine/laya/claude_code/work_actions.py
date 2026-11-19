# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Direct actions on the rows of "My work", each started by the user's click.

Linear (change state, assign to me, set priority, comment):
- One write turn of the user's `claude` CLI whose allowlist holds exactly one
  tool: the discovered `save_issue` or `save_comment`. Every other discovered
  MCP tool is denied and built-in tools are removed (cli.build_args). The
  prompt carries the tool arguments as JSON to copy verbatim; `save_issue`
  always gets the issue `id` (without it the tool creates a new issue),
  `save_comment` always gets `issueId` and never `id` (that would edit an
  existing comment).
- Then one read turn that may use only `get_issue` reads the issue back; the
  change counts as done only when Linear reports the new value.
- A team's workflow states come from one `list_issue_statuses` turn and are
  cached per team for `STATES_CACHE_SECONDS`.

GitHub (only pull requests the user authored, checked live on every action):
ready for review, back to draft, request reviewers, comment, update branch,
merge. Each is one `gh` command, no model turn. A merge names a method the
repository allows and the head commit the user confirmed
(`--match-head-commit`), so new commits pushed after the confirmation make
GitHub refuse it. `--admin` and `--auto` are never passed.

Nothing here runs on a schedule or retries: a failed action is reported and
the user decides. After an action the row's fresh data replaces it in the
My work cache (my_work.replace_cached_item), or the row is dropped when the
issue or PR is no longer open.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import structlog

from laya.claude_code import cli, discover, my_work, store

log = structlog.get_logger()

STATE_PLATFORM = "work_actions"
STATES_CACHE_SECONDS = 24 * 3600
TURN_TIMEOUT = 240.0


class ActionError(Exception):
    """An action that was refused or failed; `status` is the HTTP status to report."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# Rows with an action in flight; a second click on the same row is refused.
_busy: set[str] = set()


class _RowGuard:
    def __init__(self, key: str):
        self.key = key

    def __enter__(self) -> None:
        if self.key in _busy:
            raise ActionError(f"An action on {self.key} is already running.", 409)
        _busy.add(self.key)

    def __exit__(self, *exc: Any) -> None:
        _busy.discard(self.key)


# ── Linear ───────────────────────────────────────────────────────────────

LINEAR_ACTIONS = ("state", "assign_me", "priority", "comment")
IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9]{0,9}-\d{1,7}$")
TEAM = re.compile(r"^[A-Za-z0-9]{1,10}$")
MAX_COMMENT = 20_000

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "id": {"type": "string", "description": "id of the issue or comment the tool returned"},
        "url": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["ok", "id", "url", "error"],
}

_VERIFY_FIELDS = [
    "id", "title", "url", "status", "statusType", "priority", "project",
    "dueDate", "updatedAt", "labels", "assignee", "assigneeId",
]

_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "issue": {
            "type": "object",
            "properties": {
                **my_work._ISSUE["properties"],
                "aid": {"type": ["string", "null"], "description": "assigneeId"},
            },
            "required": ["i", "t"],
        },
    },
    "required": ["found", "issue"],
}

_STATES_SCHEMA = {
    "type": "object",
    "properties": {
        "states": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "n": {"type": "string", "description": "name"},
                    "t": {"type": "string", "description": "type"},
                },
                "required": ["id", "n", "t"],
            },
        }
    },
    "required": ["states"],
}

# Order of the state types in the state menu.
_TYPE_ORDER = {"triage": 0, "backlog": 1, "unstarted": 2, "started": 3, "completed": 4, "canceled": 5, "duplicate": 6}


def team_of(identifier: str) -> str:
    return identifier.split("-", 1)[0]


def check_identifier(identifier: str) -> str:
    identifier = (identifier or "").strip().upper()
    if not IDENTIFIER.match(identifier):
        raise ActionError(f"'{identifier}' is not a Linear issue identifier.")
    return identifier


def linear_payload(action: str, identifier: str, value: Any) -> tuple[str, dict[str, Any]]:
    """(role, tool arguments) of a Linear action. Pure; validates the value."""
    if action == "state":
        name = str(value or "").strip()
        if not name:
            raise ActionError("Choose a state.")
        return "save_issue", {"id": identifier, "state": name}
    if action == "assign_me":
        return "save_issue", {"id": identifier, "assignee": "me"}
    if action == "priority":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
            raise ActionError("Priority must be 0 (none), 1 (urgent), 2 (high), 3 (medium) or 4 (low).")
        return "save_issue", {"id": identifier, "priority": value}
    if action == "comment":
        body = str(value or "")
        if not body.strip():
            raise ActionError("The comment is empty.")
        if len(body) > MAX_COMMENT:
            raise ActionError(f"The comment is longer than {MAX_COMMENT} characters.")
        return "save_comment", {"issueId": identifier, "body": body}
    raise ActionError(f"Unknown Linear action '{action}'.")


def write_prompt(tool: str, args: dict[str, Any]) -> str:
    return (
        f"Your only task: call the tool {tool} exactly once with exactly these arguments. "
        "Copy every value verbatim: do not rewrite, shorten, translate, format or add "
        "anything, do not add arguments and do not leave any out.\n"
        f"ARGUMENTS (JSON):\n{json.dumps(args, ensure_ascii=False, indent=2)}\n"
        "Do not call any other tool, and do not call the tool a second time, even if it fails.\n"
        "Then answer with ok, id (the id the tool returns) and url (if the tool returns one); "
        "set ok to false and error to the tool's error message if the call failed, "
        "otherwise error is an empty string."
    )


def verify_prompt(tool: str, identifier: str) -> str:
    args = {"id": identifier, "fields": _VERIFY_FIELDS}
    return (
        "You read one Linear issue. Copy data exactly; do not judge or summarise.\n"
        f"Call {tool} exactly once with these arguments:\n{json.dumps(args)}\n"
        "Answer: found = whether the issue was returned; issue: i = identifier, t = title, "
        "u = url, s = status name, st = statusType, p = priority value (0-4), pr = project name, "
        "d = dueDate, up = updatedAt, l = label names, a = assignee name (null when unassigned), "
        "aid = assigneeId. Call no other tool."
    )


def states_prompt(tool: str, team: str) -> str:
    return (
        "You read a Linear team's workflow states. Copy data exactly.\n"
        f"Call {tool} exactly once with these arguments:\n{json.dumps({'team': team})}\n"
        "Answer with every status: id, n = name, t = type. Call no other tool."
    )


def check_turn(data: dict[str, Any]) -> dict[str, Any]:
    """The structured output of a turn that used only its allowed tool."""
    denials = data.get("permission_denials") or []
    if denials:
        names = sorted({str(d.get("tool_name", "?")) for d in denials if isinstance(d, dict)})
        raise ActionError(
            f"Claude Code tried tools it is not allowed to use: {', '.join(names) or 'unknown'}", 502
        )
    out = data.get("structured_output")
    if not isinstance(out, dict):
        raise ActionError("Claude Code returned no result.", 502)
    return out


def _is_me(raw: dict[str, Any], identity: dict[str, Any]) -> bool:
    mine = {
        str(v).strip().lower()
        for v in (identity.get("linear_user_id"), identity.get("email"), identity.get("name"))
        if v
    }
    return any(str(raw.get(k) or "").strip().lower() in mine for k in ("aid", "a") if raw.get(k))


def verify_change(action: str, payload: dict[str, Any], raw: dict[str, Any],
                  identity: dict[str, Any]) -> str | None:
    """None when Linear reports the change, else what it reports instead."""
    if action == "state":
        if str(raw.get("s") or "").strip().lower() != payload["state"].strip().lower():
            return f"Linear reports the state '{raw.get('s') or 'unknown'}', not '{payload['state']}'."
    elif action == "priority":
        if raw.get("p") != payload["priority"]:
            return f"Linear reports priority {raw.get('p')}, not {payload['priority']}."
    elif action == "assign_me":
        if not _is_me(raw, identity):
            return f"Linear reports the assignee '{raw.get('a') or 'nobody'}', not you."
    return None


async def _linear_context() -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """The linear source, its tools (current discovery first) and every discovered MCP tool."""
    source = await store.get_source("linear")
    if source is None:
        raise ActionError("Linear is not connected through Claude Code.", 404)
    found = discover.cached()
    if found is None or not found.all_tools:
        found = await discover.discover(with_identity=False)
    pd = found.platforms.get("linear")
    tools = {**(source.get("tools") or {}), **((pd.tools if pd else None) or {})}
    return source, tools, list(found.all_tools)


def _tool(tools: dict[str, str], role: str) -> str:
    tool = tools.get(role)
    if not tool:
        raise ActionError(
            f"Claude Code has no Linear '{role}' tool. Run discovery again in Settings → Integrations.",
            422,
        )
    return tool


def _deny(all_tools: list[str], tool: str) -> list[str]:
    return sorted(set(all_tools) - {tool})


def _model(source: dict[str, Any]) -> str:
    return store.merged_config("linear", source.get("config")).get("model") or "haiku"


def sort_states(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(states, key=lambda s: _TYPE_ORDER.get(s["type"], 9))


async def team_states(team: str, refresh: bool = False) -> list[dict[str, Any]]:
    """The team's workflow states [{id, name, type}], read once and cached."""
    if not TEAM.match(team or ""):
        raise ActionError(f"'{team}' is not a Linear team key.")
    key = f"states:{team.upper()}"
    if not refresh:
        raw = (await store.get_state(STATE_PLATFORM, key)).get(key)
        if raw:
            try:
                cached = json.loads(raw)
                if time.time() - cached.get("at", 0) < STATES_CACHE_SECONDS and cached.get("states"):
                    return cached["states"]
            except ValueError:
                pass
    source, tools, all_tools = await _linear_context()
    tool = _tool(tools, "list_issue_statuses")
    try:
        data = await cli.run_send(
            "linear_states", states_prompt(tool, team.upper()), tool,
            model=_model(source), timeout=TURN_TIMEOUT, schema=_STATES_SCHEMA,
            deny=_deny(all_tools, tool),
        )
    except cli.ClaudeCodeError as e:
        raise ActionError(f"Could not read the {team.upper()} workflow states: {e}", 502) from e
    out = check_turn(data)
    states = sort_states([
        {"id": s["id"], "name": s["n"], "type": (s.get("t") or "").lower()}
        for s in out.get("states") or [] if s.get("n")
    ])
    if not states:
        raise ActionError(f"Linear returned no workflow states for {team.upper()}.", 502)
    await store.set_state(STATE_PLATFORM, key, json.dumps({"states": states, "at": time.time()}))
    return states


async def _cached_issue(identifier: str) -> dict[str, Any] | None:
    cache = await my_work._cached("linear") or {}
    return next((r for r in cache.get("items") or [] if r.get("identifier") == identifier), None)


async def linear_action(identifier: str, action: str, value: Any = None,
                        dry_run: bool = False) -> dict[str, Any]:
    """Run one Linear action and return {ok, issue, removed, result}."""
    identifier = check_identifier(identifier)
    role, payload = linear_payload(action, identifier, value)
    if action == "state":
        names = {s["name"].lower(): s["name"] for s in await team_states(team_of(identifier))}
        if payload["state"].lower() not in names:
            raise ActionError(f"'{payload['state']}' is not a workflow state of team {team_of(identifier)}.")
        payload["state"] = names[payload["state"].lower()]

    source, tools, all_tools = await _linear_context()
    tool = _tool(tools, role)
    get_tool = _tool(tools, "get_issue")
    model = _model(source)
    prompt = write_prompt(tool, payload)
    if dry_run:
        from laya.config import get_agent_binary

        return {
            "ok": True, "dry_run": True, "tool": tool, "payload": payload,
            "command": cli.build_args(get_agent_binary("claude_code"), prompt, tools=[tool],
                                      model=model, schema=_WRITE_SCHEMA, deny=_deny(all_tools, tool)),
            "verify_command": cli.build_args(
                get_agent_binary("claude_code"), verify_prompt(get_tool, identifier),
                tools=[get_tool], model=model, schema=_VERIFY_SCHEMA, deny=_deny(all_tools, get_tool)),
        }

    with _RowGuard(identifier):
        log.info("claude_code_linear_action", identifier=identifier, action=action)
        try:
            data = await cli.run_send(
                "linear_action", prompt, tool, model=model, timeout=TURN_TIMEOUT,
                schema=_WRITE_SCHEMA, deny=_deny(all_tools, tool),
            )
        except cli.ClaudeCodeError as e:
            # Never retried: the tool may have run before the turn failed.
            raise ActionError(f"Claude Code could not complete the Linear action: {e}. "
                              "It may have been applied; refresh to check.", 502) from e
        out = check_turn(data)
        if not out.get("ok"):
            raise ActionError(f"Linear refused the change: {out.get('error') or 'the tool call failed'}", 502)
        if action == "comment" and not out.get("id"):
            raise ActionError("Claude Code reported success but returned no comment id.", 502)

        try:
            vdata = await cli.run_send(
                "linear_verify", verify_prompt(get_tool, identifier), get_tool, model=model,
                timeout=TURN_TIMEOUT, schema=_VERIFY_SCHEMA, deny=_deny(all_tools, get_tool),
            )
        except cli.ClaudeCodeError as e:
            raise ActionError(f"The change was sent but could not be checked: {e}. Refresh to see it.", 502) from e
        vout = check_turn(vdata)
        raw = vout.get("issue") or {}
        if not vout.get("found") or not raw.get("i"):
            raise ActionError("The change was sent but Linear did not return the issue. Refresh to see it.", 502)
        mismatch = verify_change(action, payload, raw, source.get("identity") or {})
        if mismatch:
            raise ActionError(f"The change did not take effect: {mismatch}", 502)

        previous = await _cached_issue(identifier) or {}
        issue = my_work._issue(raw)
        issue["identifier"] = issue["identifier"].upper()
        identity = source.get("identity") or {}
        known = any(identity.get(k) for k in ("linear_user_id", "email", "name"))
        issue["assigned_to_me"] = _is_me(raw, identity) if known else bool(previous.get("assigned_to_me"))
        issue["created_by_me"] = bool(previous.get("created_by_me"))
        for k in ("project", "due_date", "labels", "url"):
            if not issue.get(k) and previous.get(k):
                issue[k] = previous[k]
        removed = issue["state_type"] in ("completed", "canceled", "cancelled", "duplicate")
        await my_work.replace_cached_item("linear", identifier, None if removed else issue)
        return {"ok": True, "issue": issue, "removed": removed,
                "result": {"id": out.get("id"), "url": out.get("url") or None}}


# ── GitHub ───────────────────────────────────────────────────────────────

GITHUB_ACTIONS = ("ready", "draft", "reviewers", "comment", "update_branch", "merge")
MERGE_METHODS = ("squash", "merge", "rebase")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REVIEWER = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})|[A-Za-z0-9-]+/[A-Za-z0-9_.-]+)$")

PR_ONE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state%s    }
  }
}
""" % my_work.PR_FIELDS


def check_pr_ref(repo: str, number: Any) -> tuple[str, int]:
    repo = (repo or "").strip()
    if not REPO.match(repo):
        raise ActionError(f"'{repo}' is not an owner/repo name.")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ActionError("The pull request number is invalid.")
    return repo, number


def parse_reviewers(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    logins = [str(v).strip().lstrip("@") for v in items if str(v).strip()]
    if not logins:
        raise ActionError("Name at least one reviewer.")
    bad = [r for r in logins if not REVIEWER.match(r)]
    if bad:
        raise ActionError(f"Not a GitHub login or org/team: {', '.join(bad)}")
    return list(dict.fromkeys(logins))


def gh_command(action: str, repo: str, number: int, params: dict[str, Any]) -> tuple[list[str], str | None]:
    """(gh arguments, stdin) of a pull request action. Pure; validates the parameters.

    Never builds `--admin` or `--auto`.
    """
    n = str(number)
    if action == "ready":
        return ["pr", "ready", n, "-R", repo], None
    if action == "draft":
        return ["pr", "ready", n, "-R", repo, "--undo"], None
    if action == "reviewers":
        return ["pr", "edit", n, "-R", repo, "--add-reviewer", ",".join(parse_reviewers(params.get("reviewers")))], None
    if action == "comment":
        body = str(params.get("body") or "")
        if not body.strip():
            raise ActionError("The comment is empty.")
        if len(body) > 60_000:
            raise ActionError("The comment is too long for GitHub.")
        return ["pr", "comment", n, "-R", repo, "--body-file", "-"], body
    if action == "update_branch":
        return ["pr", "update-branch", n, "-R", repo], None
    if action == "merge":
        method = params.get("method")
        if method not in MERGE_METHODS:
            raise ActionError(f"Unknown merge method '{method}'.")
        sha = str(params.get("head_sha") or "").lower()
        if not SHA.match(sha):
            raise ActionError("The head commit to merge is missing or invalid.")
        args = ["pr", "merge", n, "-R", repo, f"--{method}", "--match-head-commit", sha]
        if params.get("delete_branch"):
            args.append("--delete-branch")
        return args, None
    raise ActionError(f"Unknown pull request action '{action}'.")


async def fetch_pr(repo: str, number: int) -> tuple[str, str, dict[str, Any]]:
    """(viewer login, PR state, raw PR node) read live from GitHub."""
    owner, name = repo.split("/", 1)
    try:
        data = await cli.run_gh(
            "api", "graphql", "-f", f"query={PR_ONE_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
        ) or {}
    except cli.ClaudeCodeError as e:
        raise ActionError(f"Could not read {repo}#{number}: {e}", 502) from e
    if data.get("errors"):
        raise ActionError(f"Could not read {repo}#{number}: {str(data['errors'])[:300]}", 502)
    viewer = (((data.get("data") or {}).get("viewer") or {}).get("login")) or ""
    node = (((data.get("data") or {}).get("repository") or {}).get("pullRequest")) or None
    if not node:
        raise ActionError(f"{repo}#{number} was not found.", 404)
    return viewer, node.get("state") or "", node


def check_preconditions(action: str, viewer: str, state: str, pr: dict[str, Any],
                        params: dict[str, Any]) -> None:
    """Refuse an action the live pull request does not allow."""
    ref = f"{pr['repo']}#{pr['number']}"
    if not viewer or pr["author"].lower() != viewer.lower():
        raise ActionError(f"{ref} is not your pull request; actions run only on PRs you authored.", 403)
    if state != "OPEN":
        raise ActionError(f"{ref} is {state.lower() or 'not open'}.", 409)
    if action == "ready" and not pr["draft"]:
        raise ActionError(f"{ref} is already ready for review.", 409)
    if action == "draft" and pr["draft"]:
        raise ActionError(f"{ref} is already a draft.", 409)
    if action == "reviewers":
        if any(r.lower() == viewer.lower() for r in parse_reviewers(params.get("reviewers"))):
            raise ActionError("You cannot request your own review.")
    if action == "merge":
        if pr["draft"]:
            raise ActionError(f"{ref} is a draft; mark it ready for review first.", 409)
        if params.get("method") not in pr["merge_methods"]:
            allowed = ", ".join(pr["merge_methods"]) or "none"
            raise ActionError(f"{pr['repo']} does not allow '{params.get('method')}' merges (allowed: {allowed}).")
        if str(params.get("head_sha") or "").lower() != pr["head_sha"].lower():
            raise ActionError(
                f"{ref} has new commits since you confirmed (head is now {pr['head_sha'][:7]}). "
                "Check it again before merging.", 409)


async def github_action(repo: str, number: Any, action: str, params: dict[str, Any] | None = None,
                        dry_run: bool = False) -> dict[str, Any]:
    """Run one action on the user's own pull request and return {ok, pr, removed, state}."""
    params = params or {}
    repo, number = check_pr_ref(repo, number)
    if action not in GITHUB_ACTIONS:
        raise ActionError(f"Unknown pull request action '{action}'.")
    if await store.get_source("github") is None:
        raise ActionError("GitHub is not connected through Claude Code.", 404)
    args, stdin = gh_command(action, repo, number, params)

    viewer, state, node = await fetch_pr(repo, number)
    pr = my_work.parse_pr(node)
    if pr is None:
        raise ActionError(f"{repo}#{number} was not found.", 404)
    check_preconditions(action, viewer, state, pr, params)
    if dry_run:
        return {"ok": True, "dry_run": True, "command": ["gh", *args], "stdin": stdin}

    key = f"{repo}#{number}"
    with _RowGuard(key):
        log.info("claude_code_github_action", pr=key, action=action)
        try:
            output = await cli.run_gh_write(*args, stdin=stdin)
        except cli.ClaudeCodeError as e:
            raise ActionError(str(e), 502) from e

        try:
            _, new_state, new_node = await fetch_pr(repo, number)
        except ActionError as e:
            return {"ok": True, "pr": None, "removed": False, "state": None, "output": output,
                    "note": f"Done, but the pull request could not be read again: {e}"}
        fresh = my_work.parse_pr(new_node)
        removed = new_state != "OPEN"
        await my_work.replace_cached_item("github", key, None if removed or fresh is None else fresh)
        return {"ok": True, "pr": fresh, "removed": removed, "state": new_state, "output": output}
