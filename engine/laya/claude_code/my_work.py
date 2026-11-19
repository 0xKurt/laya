# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

""""My work": the user's open Linear issues and open GitHub pull requests.

- GitHub (only when the github source is connected): the open pull requests
  the user authored (`author:@me`, nothing else), limited to the source's
  `orgs` (empty = all), read with one batched `gh api graphql` search per page
  of 25: repository, draft, base branch, mergeable state, review decision,
  requested reviewers, latest reviews, the status-check rollup with failing /
  pending counts, unresolved review threads and the Linear key named in the
  title or branch. No model turn.
- Linear (only when the linear source is connected): one read-only `claude -p`
  turn that may call only the discovered `list_issues` tool. It lists the
  issues assigned to the user and the issues the user created, in every open
  state type (triage, backlog, unstarted, started); the code merges both lists
  into one row per issue with the flags `assigned_to_me` / `created_by_me`.
- Each section is cached in `claude_code_state` (platform `my_work`) for
  `CACHE_SECONDS`; `refresh=True` skips the cache. While the agent budget is
  paused the Linear turn does not run: the cached section is returned with a
  note. A failing section reports its error next to its last cached data, so
  the endpoint never fails as a whole.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import structlog

from laya.claude_code import cli, store

log = structlog.get_logger()

STATE_PLATFORM = "my_work"
CACHE_SECONDS = 600

# GitHub search pages: 25 pull requests per page, at most 4 pages.
PAGE_SIZE = 25
MAX_PAGES = 4

# Linear: per list call, and how many extra pages the turn may read.
LINEAR_LIMIT = 100
LINEAR_OPEN_STATE_TYPES = ("started", "unstarted", "backlog", "triage")
LINEAR_TIMEOUT = 300.0

_locks: dict[str, asyncio.Lock] = {}


def _lock(section: str) -> asyncio.Lock:
    return _locks.setdefault(section, asyncio.Lock())


# ── GitHub ───────────────────────────────────────────────────────────────

PR_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: %d, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt updatedAt
        baseRefName headRefName mergeable reviewDecision
        repository { nameWithOwner }
        reviewRequests { totalCount }
        latestReviews(first: 20) { nodes { state author { login } } }
        reviewThreads(first: 100) { nodes { isResolved } }
        commits(last: 1) { nodes { commit { statusCheckRollup {
          state
          contexts(first: 100) { nodes {
            __typename
            ... on CheckRun { status conclusion }
            ... on StatusContext { state }
          } }
        } } } }
      }
    }
  }
}
""" % PAGE_SIZE

_FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_CI_STATES = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "ERROR": "failure",
    "PENDING": "pending",
    "EXPECTED": "pending",
}

# Team-key shaped words that are not Linear keys (ERC-20, UTF-8, ...).
_NOT_LINEAR = {"ERC", "EIP", "UTF", "SHA", "ISO", "RFC", "CVE", "BIP", "SIP", "AES", "TLS", "HTTP", "X"}
_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{0,9})-(\d{1,6})(?![0-9])")


def search_query(orgs: list[str]) -> str:
    """Only the user's own open pull requests; several `org:` qualifiers are OR-ed."""
    q = "is:pr is:open author:@me archived:false"
    return " ".join([q, *(f"org:{o}" for o in orgs if o)])


def linear_key(*texts: str | None) -> str | None:
    """The first Linear issue key (TRAD-123) in the title, then the branch name.

    Branch names are usually lower case (`kurt/trad-123-fix`), so case is
    ignored and the key is upper-cased.
    """
    for text in texts:
        for m in _KEY.finditer(text or ""):
            team = m.group(1).upper()
            if team in _NOT_LINEAR or team.isdigit():
                continue
            return f"{team}-{int(m.group(2))}"
    return None


def checks_summary(rollup: dict[str, Any] | None) -> dict[str, Any]:
    if not rollup:
        return {"state": "none", "failing": 0, "pending": 0, "total": 0}
    failing = pending = total = 0
    for node in ((rollup.get("contexts") or {}).get("nodes") or []):
        total += 1
        if node.get("__typename") == "CheckRun":
            if node.get("status") != "COMPLETED":
                pending += 1
            elif node.get("conclusion") in _FAILED_CONCLUSIONS:
                failing += 1
        else:
            state = node.get("state")
            if state in ("FAILURE", "ERROR"):
                failing += 1
            elif state in ("PENDING", "EXPECTED"):
                pending += 1
    return {
        "state": _CI_STATES.get(rollup.get("state") or "", "none"),
        "failing": failing,
        "pending": pending,
        "total": total,
    }


def parse_pr(node: dict[str, Any]) -> dict[str, Any] | None:
    if not node or not node.get("number"):
        return None
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") if commits else None
    reviews = [
        {"state": r.get("state"), "author": ((r.get("author") or {}).get("login") or "")}
        for r in ((node.get("latestReviews") or {}).get("nodes") or [])
    ]
    threads = (node.get("reviewThreads") or {}).get("nodes") or []
    mergeable = node.get("mergeable") or "UNKNOWN"
    return {
        "repo": (node.get("repository") or {}).get("nameWithOwner") or "",
        "number": node["number"],
        "title": node.get("title") or "",
        "url": node.get("url") or "",
        "draft": bool(node.get("isDraft")),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "base": node.get("baseRefName") or "",
        "branch": node.get("headRefName") or "",
        "mergeable": mergeable,
        "conflicts": mergeable == "CONFLICTING",
        "review_decision": node.get("reviewDecision"),
        "requested_reviewers": (node.get("reviewRequests") or {}).get("totalCount") or 0,
        "reviews": reviews,
        "approvals": sum(1 for r in reviews if r["state"] == "APPROVED"),
        "changes_requested": sum(1 for r in reviews if r["state"] == "CHANGES_REQUESTED"),
        "unresolved_threads": sum(1 for t in threads if not t.get("isResolved")),
        "checks": checks_summary(rollup),
        "linear_key": linear_key(node.get("title"), node.get("headRefName")),
    }


async def fetch_github(config: dict[str, Any]) -> list[dict[str, Any]]:
    query = search_query(config.get("orgs") or [])
    prs: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        args = ["api", "graphql", "-f", f"query={PR_QUERY}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        data = await cli.run_gh(*args) or {}
        if data.get("errors"):
            raise cli.ClaudeCodeError(str(data["errors"])[:300])
        search = (data.get("data") or {}).get("search") or {}
        prs += [p for p in (parse_pr(n) for n in search.get("nodes") or []) if p]
        page = search.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
    return prs


# ── Linear ───────────────────────────────────────────────────────────────

LINEAR_FIELDS = [
    "id", "title", "url", "status", "statusType", "priority", "project",
    "dueDate", "updatedAt", "labels", "assignee",
]

_ISSUE = {
    "type": "object",
    "properties": {
        "i": {"type": "string", "description": "identifier, e.g. TRAD-12 (the id field)"},
        "t": {"type": "string", "description": "title"},
        "u": {"type": ["string", "null"], "description": "url"},
        "s": {"type": ["string", "null"], "description": "status name"},
        "st": {"type": ["string", "null"], "description": "statusType"},
        "p": {"type": ["integer", "null"], "description": "priority value 0-4"},
        "pr": {"type": ["string", "null"], "description": "project"},
        "d": {"type": ["string", "null"], "description": "dueDate"},
        "up": {"type": ["string", "null"], "description": "updatedAt"},
        "l": {"type": "array", "items": {"type": "string"}, "description": "label names"},
        "a": {"type": ["string", "null"], "description": "assignee name"},
    },
    "required": ["i", "t"],
}

LINEAR_SCHEMA = {
    "type": "object",
    "properties": {
        "assigned": {"type": "array", "items": _ISSUE},
        "created": {"type": "array", "items": _ISSUE},
    },
    "required": ["assigned", "created"],
}


def linear_prompt(tool: str) -> str:
    states = ", ".join(f'"{s}"' for s in LINEAR_OPEN_STATE_TYPES)
    fields = json.dumps(LINEAR_FIELDS)
    return f"""You are a data collector. Copy data exactly; do not judge or summarise.
Call {tool} {2 * len(LINEAR_OPEN_STATE_TYPES)} times in parallel in ONE step: once per state in
[{states}] with assignee="me", and once per state in [{states}] with creator="me".
Every call: limit={LINEAR_LIMIT}, fields={fields}.
Only if a response has hasNextPage=true, call it again with its cursor (at most 2 more pages).
Answer: "assigned" = every issue of the assignee="me" calls, "created" = every issue of the
creator="me" calls. Per issue: i = id (the identifier), t = title, u = url, s = status,
st = statusType, p = priority.value, pr = project, d = dueDate, up = updatedAt,
l = label names, a = assignee (null when unassigned).
Call no other tool. Return {{"assigned": [], "created": []}} if there is nothing."""


def _issue(it: dict[str, Any]) -> dict[str, Any]:
    priority = it.get("p")
    return {
        "identifier": (it.get("i") or "").strip(),
        "title": it.get("t") or "",
        "url": it.get("u") or None,
        "state": it.get("s") or "",
        "state_type": (it.get("st") or "").lower(),
        "priority": priority if isinstance(priority, int) and 0 <= priority <= 4 else 0,
        "project": it.get("pr") or None,
        "cycle": None,
        "due_date": it.get("d") or None,
        "updated_at": it.get("up") or None,
        "labels": [str(x) for x in (it.get("l") or []) if x],
        "assignee": it.get("a") or None,
        "assigned_to_me": False,
        "created_by_me": False,
    }


def merge_issues(out: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per open issue with the assigned / created flags of both lists."""
    rows: dict[str, dict[str, Any]] = {}
    for flag, key in (("assigned_to_me", "assigned"), ("created_by_me", "created")):
        for raw in out.get(key) or []:
            issue = _issue(raw)
            ident = issue["identifier"]
            if not ident or issue["state_type"] in ("completed", "canceled", "cancelled"):
                continue
            row = rows.setdefault(ident, issue)
            for k, v in issue.items():
                if row.get(k) in (None, "", []) and v not in (None, "", []):
                    row[k] = v
            row[flag] = True
    return list(rows.values())


async def fetch_linear(source: dict[str, Any]) -> list[dict[str, Any]]:
    from laya.claude_code.service import _with_deny_list

    tool = (source.get("tools") or {}).get("list_issues")
    if not tool:
        raise cli.ClaudeCodeError("No Linear list-issues tool was discovered.")
    source = await _with_deny_list(source)
    out = await cli.run_collect(
        "linear_my_work", linear_prompt(tool), LINEAR_SCHEMA, [tool],
        model=(source.get("config") or {}).get("model") or "haiku",
        timeout=LINEAR_TIMEOUT,
        deny=source.get("deny_tools") or [],
    )
    return merge_issues(out)


# ── cache and entry point ────────────────────────────────────────────────

_ITEMS_KEY = {"github": "prs", "linear": "issues"}


async def _cached(section: str) -> dict[str, Any] | None:
    raw = (await store.get_state(STATE_PLATFORM, section)).get(section)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _age_seconds(fetched_at: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at or "")).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _section(section: str, items: list | None, *, connected: bool, fetched_at: str | None = None,
             error: str | None = None, note: str | None = None, cached: bool = False) -> dict[str, Any]:
    return {
        _ITEMS_KEY[section]: items or [],
        "connected": connected,
        "error": error,
        "note": note,
        "fetched_at": fetched_at,
        "cached": cached,
    }


async def _load(section: str, refresh: bool) -> dict[str, Any]:
    source = await store.get_source(section)
    if source is None:
        return _section(section, [], connected=False)

    async with _lock(section):
        cache = await _cached(section)
        if cache and not refresh and _age_seconds(cache.get("fetched_at")) < CACHE_SECONDS:
            return _section(section, cache.get("items"), connected=True,
                            fetched_at=cache.get("fetched_at"), cached=True)

        if section == "linear":
            from laya.pipeline.agent_budget import is_agent_budget_paused

            if await is_agent_budget_paused():
                return _section(
                    section, (cache or {}).get("items"), connected=True,
                    fetched_at=(cache or {}).get("fetched_at"), cached=cache is not None,
                    note="The agent usage budget is paused; showing the last fetched issues.",
                )

        try:
            items = (
                await fetch_github(source.get("config") or {})
                if section == "github"
                else await fetch_linear(source)
            )
        except Exception as e:  # a section's failure is reported, never raised
            log.warning("claude_code_my_work_failed", section=section, error=str(e))
            return _section(section, (cache or {}).get("items"), connected=True,
                            fetched_at=(cache or {}).get("fetched_at"), cached=cache is not None,
                            error=str(e)[:300] or type(e).__name__)

        fetched_at = store.now_iso()
        await store.set_state(STATE_PLATFORM, section,
                              json.dumps({"items": items, "fetched_at": fetched_at}))
        return _section(section, items, connected=True, fetched_at=fetched_at)


async def get_my_work(refresh: bool = False) -> dict[str, Any]:
    github, linear = await asyncio.gather(_load("github", refresh), _load("linear", refresh))
    return {"github": github, "linear": linear}
