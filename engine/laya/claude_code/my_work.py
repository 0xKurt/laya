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
- Each section is stored in `claude_code_state` (platform `my_work`) with
  `fetched_at` (last success), `attempted_at` (last try) and `error` (the
  last try's failure, None after a success). Reads return the stored
  sections at once and never wait for a fetch: a refresh always runs in the
  background (`start_refresh`), under `SECTION_TIMEOUT`, and announces its
  result with a `my_work_updated` websocket message. The service loop
  (service.py) starts due refreshes during the source's active hours; a read
  starts one when the section is stale, `refresh=True` always. While the
  agent budget is paused the Linear turn does not run and the view gets a
  note.
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

# How old a section's last refresh may get before it is refreshed again:
# GitHub costs one `gh` call, Linear a model turn.
REFRESH_SECONDS = {"github": 600, "linear": 1800}

# GitHub search pages: 25 pull requests per page, at most 4 pages.
PAGE_SIZE = 25
MAX_PAGES = 4

# Linear: per list call, and how many extra pages the turn may read.
LINEAR_LIMIT = 100
LINEAR_OPEN_STATE_TYPES = ("started", "unstarted", "backlog", "triage")
LINEAR_TIMEOUT = 300.0

# Upper bound of one section refresh, including picking the Claude account
# and its tool listing, so a hung `gh` or `claude` can never keep a section
# refreshing.
SECTION_TIMEOUT = {"github": 180.0, "linear": LINEAR_TIMEOUT + 120.0}

# Guards the read-modify-write of a stored section; never held during a fetch.
_locks: dict[str, asyncio.Lock] = {}
# The running background refresh per section.
_tasks: dict[str, asyncio.Task] = {}
# Rows an action replaced while the section's refresh runs: key -> row (None = dropped).
_row_updates: dict[str, dict[str, dict[str, Any] | None]] = {}


def _lock(section: str) -> asyncio.Lock:
    return _locks.setdefault(section, asyncio.Lock())


# ── GitHub ───────────────────────────────────────────────────────────────

# The fields read per pull request; shared with the single-PR refresh after
# an action (work_actions.py).
PR_FIELDS = """
        number title url isDraft createdAt updatedAt
        baseRefName headRefName headRefOid mergeable mergeStateStatus reviewDecision
        author { login }
        repository {
          nameWithOwner mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed
          deleteBranchOnMerge
        }
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
"""

PR_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: %d, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {%s      }
    }
  }
}
""" % (PAGE_SIZE, PR_FIELDS)

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


def merge_methods(repo: dict[str, Any]) -> list[str]:
    """The merge methods the repository allows, in `gh pr merge` flag names."""
    allowed = [
        ("squash", repo.get("squashMergeAllowed")),
        ("merge", repo.get("mergeCommitAllowed")),
        ("rebase", repo.get("rebaseMergeAllowed")),
    ]
    return [name for name, ok in allowed if ok]


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
    repo = node.get("repository") or {}
    return {
        "repo": repo.get("nameWithOwner") or "",
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
        "author": ((node.get("author") or {}).get("login") or ""),
        "head_sha": node.get("headRefOid") or "",
        "merge_state": node.get("mergeStateStatus") or "UNKNOWN",
        "merge_methods": merge_methods(repo),
        "delete_branch_on_merge": bool(repo.get("deleteBranchOnMerge")),
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
    """The user's open Linear issues, read on a login that has Linear
    (``accounts.pick_for_platform``) with that account's own tool name."""
    from laya.claude_code import accounts

    assignment = await accounts.pick_for_platform("linear", ("list_issues",))
    if assignment is None:
        raise cli.ClaudeCodeError(await accounts.platform_unavailable_reason("linear", ("list_issues",)))
    limit = await accounts.limit_reached(assignment.account_id)
    if limit:
        raise cli.ClaudeCodeError(limit)
    tool = assignment.tools["list_issues"]
    out = await cli.run_collect(
        "linear_my_work", linear_prompt(tool), LINEAR_SCHEMA, [tool],
        model=(source.get("config") or {}).get("model") or cli.AUTOMATIC_MODEL,
        timeout=LINEAR_TIMEOUT,
        deny=assignment.all_tools,
        env=assignment.env or None,
        account_id=assignment.account_id,
        agent_id=assignment.agent_id,
    )
    return merge_issues(out)


# ── stored sections, background refresh and entry point ──────────────────

_ITEMS_KEY = {"github": "prs", "linear": "issues"}
SECTIONS = ("github", "linear")


async def _cached(section: str) -> dict[str, Any] | None:
    raw = (await store.get_state(STATE_PLATFORM, section)).get(section)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _age_seconds(stamp: str | None, now: datetime | None = None) -> float:
    try:
        return ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(stamp or "")).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _last_attempt(cache: dict[str, Any] | None) -> str | None:
    """When the section was last refreshed, successfully or not."""
    cache = cache or {}
    return cache.get("attempted_at") or cache.get("fetched_at")


def is_stale(section: str, cache: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """Whether the section's last refresh attempt is older than its interval.

    The attempt (not the last success) counts, so a failing section is
    retried once per interval instead of on every read.
    """
    return _age_seconds(_last_attempt(cache), now) >= REFRESH_SECONDS[section]


def is_refreshing(section: str) -> bool:
    task = _tasks.get(section)
    return task is not None and not task.done()


def _section(section: str, items: list | None, *, connected: bool, fetched_at: str | None = None,
             error: str | None = None, note: str | None = None, cached: bool = False,
             refreshing: bool = False, attempted_at: str | None = None) -> dict[str, Any]:
    return {
        _ITEMS_KEY[section]: items or [],
        "connected": connected,
        "error": error,
        "note": note,
        "fetched_at": fetched_at,
        "attempted_at": attempted_at,
        "cached": cached,
        "refreshing": refreshing,
    }


async def _budget_paused() -> bool:
    from laya.pipeline.agent_budget import is_agent_budget_paused

    return await is_agent_budget_paused()


def describe_error(section: str, e: BaseException) -> str:
    """A section error as the view shows it: the reason, readable."""
    from laya.claude_code import accounts

    if isinstance(e, asyncio.TimeoutError):
        return (f"Timed out after {int(SECTION_TIMEOUT[section])}s; "
                "showing the last fetched data. Laya tries again at the next refresh.")
    text = str(e)[:300] or type(e).__name__
    if section == "linear" and accounts.is_rejection(None, text) and "limit reached" not in text.lower():
        return f"Usage limit reached on the login: {text}"
    return text


async def _fetch(section: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    if section == "github":
        return await fetch_github(source.get("config") or {})
    return await fetch_linear(source)


async def refresh_section(section: str) -> None:
    """Fetch one section and store it with its outcome. Never raises.

    The fetch runs under `SECTION_TIMEOUT`, so a hung `gh` or `claude` turn
    ends as a stored error. The section lock is held only to write the
    result, never during the fetch, so reads and row updates after actions
    never wait for a fetch. Rows an action replaced while the fetch ran are
    put over the fetched list (they are newer).
    """
    _row_updates.setdefault(section, {})
    source = await store.get_source(section)
    if source is None:
        _row_updates.pop(section, None)
        return
    if section == "linear" and await _budget_paused():
        # The view shows the paused note; the stored issues stay as they are.
        _row_updates.pop(section, None)
        return
    started = store.now_iso()
    items: list[dict[str, Any]] | None = None
    error: str | None = None
    try:
        items = await asyncio.wait_for(_fetch(section, source), timeout=SECTION_TIMEOUT[section])
    except asyncio.CancelledError:
        _row_updates.pop(section, None)
        raise
    except Exception as e:  # a section's failure is stored, never raised
        error = describe_error(section, e)
        log.warning("claude_code_my_work_failed", section=section, error=error)
    updates = _row_updates.pop(section, {})
    async with _lock(section):
        cache = await _cached(section) or {}
        if items is None:
            record = {**cache, "items": cache.get("items") or [], "error": error, "attempted_at": started}
        else:
            for key, row in updates.items():
                items = _put_row(section, items, key, row)
            record = {"items": items, "fetched_at": store.now_iso(), "attempted_at": started, "error": None}
        await store.set_state(STATE_PLATFORM, section, json.dumps(record))
    await _broadcast(section)


async def _broadcast(section: str) -> None:
    """Tell open views that a section's stored data changed."""
    try:
        from laya.api.websocket import manager

        await manager.broadcast({"type": "my_work_updated", "section": section})
    except Exception as e:  # a closed socket must not fail the refresh
        log.debug("claude_code_my_work_broadcast_failed", error=str(e))


def start_refresh(section: str) -> bool:
    """Start a background refresh of the section unless one is running.

    Returns whether a refresh was started. The caller never waits for it.
    """
    if is_refreshing(section):
        return False
    # Collect action rows from now on, before the task first runs.
    _row_updates[section] = {}

    async def _run() -> None:
        try:
            await refresh_section(section)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # refresh_section already stores its errors
            log.warning("claude_code_my_work_refresh_crashed", section=section, error=str(e))

    _tasks[section] = asyncio.create_task(_run(), name=f"claude_code_my_work_{section}")
    return True


async def run_due_refreshes(now: datetime | None = None) -> list[str]:
    """Start the background refresh of every section that is due now.

    Due: the platform is connected, `now` is inside its source's active
    hours, the last attempt is older than the section's interval, and for
    Linear the agent budget is not paused. Returns the sections started.
    """
    from laya.claude_code.service import within_active_hours

    now = now or datetime.now(timezone.utc)
    started: list[str] = []
    for section in SECTIONS:
        source = await store.get_source(section)
        if source is None or is_refreshing(section):
            continue
        if not within_active_hours(source.get("config") or {}, now):
            continue
        if not is_stale(section, await _cached(section), now):
            continue
        if section == "linear" and await _budget_paused():
            continue
        if start_refresh(section):
            started.append(section)
    return started


async def _view(section: str, refresh: bool) -> dict[str, Any]:
    source = await store.get_source(section)
    if source is None:
        return _section(section, [], connected=False)
    cache = await _cached(section)
    if refresh or is_stale(section, cache):
        start_refresh(section)
    note = None
    if section == "linear" and await _budget_paused():
        note = "The agent usage budget is paused; showing the last fetched issues."
    cache = cache or {}
    return _section(
        section, cache.get("items"), connected=True,
        fetched_at=cache.get("fetched_at"), attempted_at=cache.get("attempted_at"),
        error=cache.get("error"), note=note, cached=bool(cache),
        refreshing=is_refreshing(section),
    )


def item_key(section: str, item: dict[str, Any]) -> str:
    """The identity of a cached row: `owner/repo#N` for a PR, the identifier for an issue."""
    if section == "github":
        return f"{item.get('repo', '')}#{item.get('number', '')}"
    return str(item.get("identifier") or "")


def _put_row(section: str, rows: list[dict[str, Any]], key: str,
             item: dict[str, Any] | None) -> list[dict[str, Any]]:
    """`rows` with the row `key` replaced by `item`, appended, or dropped (`None`)."""
    rows = list(rows)
    index = next((i for i, r in enumerate(rows) if item_key(section, r) == key), None)
    if item is None:
        if index is not None:
            rows.pop(index)
    elif index is None:
        rows.append(item)
    else:
        rows[index] = item
    return rows


async def replace_cached_item(section: str, key: str, item: dict[str, Any] | None) -> None:
    """Put one row's fresh data into the section's cache, or drop the row.

    An action changes one issue or PR; work_actions reads that row back and
    calls this, so the view shows the row's new state at once without a full
    refresh. `item=None` removes the row (a merged PR, a completed issue).
    A refresh running at the same time gets the row too, so its older data
    does not overwrite it. Nothing is written when the section has no cache yet.
    """
    if section in _row_updates:
        _row_updates[section][key] = item
    async with _lock(section):
        cache = await _cached(section)
        if not cache:
            return
        cache["items"] = _put_row(section, cache.get("items") or [], key, item)
        await store.set_state(STATE_PLATFORM, section, json.dumps(cache))
    await _broadcast(section)


async def get_my_work(refresh: bool = False) -> dict[str, Any]:
    """The stored sections, at once. Never waits for a fetch.

    A stale section (or every section with `refresh=True`) gets a background
    refresh; its `refreshing` flag is true until the refresh has stored its
    result, which is also announced as a `my_work_updated` websocket message.
    """
    github, linear = await asyncio.gather(_view("github", refresh), _view("linear", refresh))
    return {"github": github, "linear": linear}


def reset() -> None:
    """Forget running refreshes and locks (tests; each test has its own loop)."""
    for task in _tasks.values():
        if not task.done():
            task.cancel()
    _tasks.clear()
    _row_updates.clear()
    _locks.clear()
