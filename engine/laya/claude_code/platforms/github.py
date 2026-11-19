# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""GitHub collector through the `gh` CLI (no LLM turn).

- Participating notifications on pull requests and issues. With
  `others_prs_only_on_mention` (default), another person's pull request only
  counts when the user is @-mentioned there; the user's own pull requests and
  issues keep every comment. `raw_event_type` is the notification reason, so
  the seeded rule drops review requests, state changes and CI.
- The latest comment's author is the actor, so the user's own comment is a
  self event and a bot's comment is dropped by the seeded bot rule.
- The user's pull requests merged or closed since the cursor become closing
  events (`resolved_in_source`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from laya.claude_code.cli import ClaudeCodeError, run_gh
from laya.claude_code.platforms.common import make_event, parse_ts
from laya.models.event import LayaEvent

log = structlog.get_logger()

PLATFORM = "github"

_SUBJECT_TYPES = {"PullRequest": "pull_request", "Issue": "ticket"}


def html_url(api_url: str | None) -> str | None:
    """https://api.github.com/repos/o/r/pulls/5 -> https://github.com/o/r/pull/5"""
    if not api_url:
        return None
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    return url.replace("/pulls/", "/pull/")


def _number(api_url: str | None) -> str:
    return (api_url or "").rstrip("/").rsplit("/", 1)[-1]


async def _pr_author(api_url: str) -> str:
    try:
        pr = await run_gh("api", api_url)
    except ClaudeCodeError:
        return ""
    return (((pr or {}).get("user") or {}).get("login") or "").lower()


async def notification_event(
    n: dict[str, Any], me: str, config: dict[str, Any]
) -> LayaEvent | None:
    subject = n.get("subject") or {}
    subject_type = _SUBJECT_TYPES.get(subject.get("type") or "")
    if subject_type is None:
        return None
    repo = n["repository"]["full_name"]
    orgs = {o.lower() for o in config.get("orgs") or []}
    if orgs and n["repository"]["owner"]["login"].lower() not in orgs:
        return None
    reason = n.get("reason") or "notification"
    if (
        subject_type == "pull_request"
        and config.get("others_prs_only_on_mention", True)
        and reason not in ("mention", "author")
        and await _pr_author(subject.get("url") or "") != me.lower()
    ):
        return None

    number = _number(subject.get("url"))
    body, actor, url = "", "", html_url(subject.get("url"))
    comment_url = subject.get("latest_comment_url")
    if comment_url and comment_url != subject.get("url"):
        try:
            comment = await run_gh("api", comment_url) or {}
            body = comment.get("body") or ""
            actor = (comment.get("user") or {}).get("login") or ""
            url = comment.get("html_url") or url
        except ClaudeCodeError as e:
            log.debug("claude_code_github_comment_failed", error=str(e))
    title = subject.get("title") or ""
    return make_event(
        platform=PLATFORM,
        native_id=f"notification:{n['id']}:{n['updated_at']}",
        timestamp=parse_ts(n["updated_at"]),
        raw_event_type=reason,
        actor_name=actor,
        actor_email="",
        actor_handle=actor or None,
        subject_type=subject_type,
        subject_id=f"{repo}#{number}",
        subject_title=f"{repo}#{number}: {title}",
        subject_url=url,
        body=body or f"{reason}: {title}",
        metadata={"github_repo": repo, "github_reason": reason},
    )


def closed_pr_event(pr: dict[str, Any], me: str) -> LayaEvent:
    repo = pr["repository_url"].replace("https://api.github.com/repos/", "")
    merged_at = (pr.get("pull_request") or {}).get("merged_at")
    when = merged_at or pr.get("closed_at")
    state = "merged" if merged_at else "closed"
    return make_event(
        platform=PLATFORM,
        native_id=f"{state}:{repo}#{pr['number']}",
        timestamp=parse_ts(when),
        raw_event_type=f"pr_{state}",
        actor_name=me,
        actor_email="",
        actor_handle=me,
        subject_type="pull_request",
        subject_id=f"{repo}#{pr['number']}",
        subject_title=f"{repo}#{pr['number']}: {pr.get('title') or ''}",
        subject_url=pr.get("html_url"),
        body=f"Pull request {state}.",
        metadata={"github_repo": repo, "github_reason": state, "resolved_in_source": True},
    )


async def collect(source: dict[str, Any], since: datetime) -> list[LayaEvent]:
    config = source.get("config") or {}
    me = (source.get("identity") or {}).get("github_login")
    if not me:
        me = ((await run_gh("api", "user")) or {}).get("login") or ""
    if not me:
        raise ClaudeCodeError("GitHub login unknown; run `gh auth login`")
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    events: list[LayaEvent] = []
    pages = await run_gh(
        "api", "--paginate", "--slurp",
        f"notifications?all=true&participating=true&since={since_iso}",
    )
    for page in pages or []:
        for n in page or []:
            event = await notification_event(n, me, config)
            if event is not None:
                events.append(event)

    orgs = " ".join(f"org:{o}" for o in sorted(config.get("orgs") or []))
    found = await run_gh(
        "api", "-X", "GET", "search/issues",
        "-f", f"q=is:pr is:closed author:{me} closed:>={since_iso} {orgs}".strip(),
        "-f", "per_page=50",
    )
    for pr in (found or {}).get("items", []):
        events.append(closed_pr_event(pr, me))
    return events
