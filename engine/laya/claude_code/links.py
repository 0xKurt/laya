# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Linked documents: a short summary of what an event links to.

An enricher (platforms/common.py `register_enricher`) that runs over the
Slack, Gmail, Linear and Notion events of every sweep. It finds links to
Notion pages, Google Docs / Drive files and GitHub issues / pull requests in
an event's body, reads each one read-only and appends a section

    Linked documents:
    - <title> (<link>): <2-4 sentence summary or the relevant excerpt>

to the body, so a card can be judged without opening the documents.

Bounds:
- at most `MAX_LINKS` links per event (the event's own page is skipped);
- every result (also "could not be read") is cached by link in
  `claude_code_state` (platform `links`) for `CACHE_HOURS`;
- GitHub is read with `gh api` (no model turn); Notion pages and Google
  files of one event are read in one batched `claude -p` turn that may only
  use the discovered Notion fetch tool and the Drive read tool;
- while the agent budget is paused no model turn runs (cached results and
  GitHub excerpts are still added);
- per platform switch `linked_documents` (default true).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from laya.claude_code import cli, store
from laya.claude_code.platforms.common import register_enricher
from laya.models.event import LayaEvent

log = structlog.get_logger()

PLATFORMS = ("slack", "gmail", "linear", "notion")
MAX_LINKS = 3
CACHE_HOURS = 24
SUMMARY_CHARS = 600
EXCERPT_CHARS = 400
STATE_PLATFORM = "links"
SECTION = "Linked documents:"

# A link ends at whitespace or at the delimiters Slack (`<url|label>`) and
# Markdown (`[label](url)`) put around it.
_URL_END = r"[^\s<>|()\[\]\"']"
_NOTION = re.compile(rf"https?://(?:www\.|app\.)?notion\.(?:so|site|com)/{_URL_END}*?([0-9a-f]{{32}})(?![0-9a-f])", re.I)
_NOTION_DASHED = re.compile(
    rf"https?://(?:www\.|app\.)?notion\.(?:so|site|com)/{_URL_END}*?"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I,
)
_GOOGLE = re.compile(
    r"https?://(?:docs|drive)\.google\.com/"
    r"(?:(?:document|spreadsheets|presentation|forms|file)/(?:u/\d+/)?d/|open\?id=|drive/(?:u/\d+/)?folders/)"
    rf"([A-Za-z0-9_-]{{10,}}){_URL_END}*"
)
_GITHUB = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(issues|pull)/(\d+)")


def extract_links(text: str, skip: str | None = None) -> list[dict[str, str]]:
    """The supported links of a text in order of appearance, deduplicated by
    document. Each is {kind, key, id, url}; `skip` is a link to leave out
    (the event's own page)."""
    found: list[tuple[int, dict[str, str]]] = []
    for rx in (_NOTION, _NOTION_DASHED):
        for m in rx.finditer(text or ""):
            page = m.group(1).replace("-", "").lower()
            found.append((m.start(), {"kind": "notion", "key": f"notion:{page}", "id": page, "url": m.group(0)}))
    for m in _GOOGLE.finditer(text or ""):
        found.append((m.start(), {"kind": "google", "key": f"google:{m.group(1)}", "id": m.group(1),
                                  "url": m.group(0).rstrip(".,;:!?")}))
    for m in _GITHUB.finditer(text or ""):
        owner, repo, kind, number = m.groups()
        repo = repo.removesuffix(".git")
        found.append((m.start(), {
            "kind": "github", "key": f"github:{owner}/{repo}#{number}".lower(),
            "id": f"{owner}/{repo}/{number}",
            "url": f"https://github.com/{owner}/{repo}/{kind}/{number}",
        }))
    found.sort(key=lambda x: x[0])
    skip_keys = {link["key"] for link in extract_links(skip)} if skip else set()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, link in found:
        if link["key"] in seen or link["key"] in skip_keys:
            continue
        seen.add(link["key"])
        out.append(link)
    return out


# ── cache ────────────────────────────────────────────────────────────────


async def cached(keys: list[str]) -> dict[str, dict[str, Any]]:
    """Fresh cache entries by link key; expired entries are deleted."""
    if not keys:
        return {}
    state = await store.get_state(STATE_PLATFORM, "link:")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CACHE_HOURS)
    fresh: dict[str, dict[str, Any]] = {}
    expired: list[str] = []
    for k, v in state.items():
        try:
            entry = json.loads(v)
            at = datetime.fromisoformat(entry["at"])
        except (ValueError, KeyError, TypeError):
            expired.append(k)
            continue
        if at < cutoff:
            expired.append(k)
        elif k[len("link:"):] in keys:
            fresh[k[len("link:"):]] = entry
    await store.delete_state(STATE_PLATFORM, expired)
    return fresh


async def remember(key: str, title: str, summary: str) -> dict[str, Any]:
    entry = {"at": datetime.now(timezone.utc).isoformat(), "title": title, "summary": summary}
    await store.set_state(STATE_PLATFORM, f"link:{key}", json.dumps(entry))
    return entry


# ── readers ──────────────────────────────────────────────────────────────


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The prose start of a Markdown description: without comments, tables,
    code fences and heading markers."""
    lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("<!--", "|", "```")):
            continue
        ln = ln.lstrip("#").strip()
        if ln:
            lines.append(ln)
    joined = " ".join(lines)
    return joined if len(joined) <= limit else joined[: limit - 1].rstrip() + "…"


async def read_github(link: dict[str, str]) -> tuple[str, str]:
    """(title, excerpt) of a GitHub issue or pull request, via `gh api`."""
    owner, repo, number = link["id"].split("/")
    data = await cli.run_gh("api", f"repos/{owner}/{repo}/issues/{number}", timeout=30) or {}
    is_pr = bool(data.get("pull_request"))
    state = data.get("state") or ""
    if is_pr and (data.get("pull_request") or {}).get("merged_at"):
        state = "merged"
    kind = "PR" if is_pr else "Issue"
    title = f"{kind} {owner}/{repo}#{number}: {data.get('title') or ''}".strip()
    summary = f"{state.capitalize()}." if state else ""
    body = _excerpt(data.get("body") or "")
    return title, f"{summary} {body}".strip()


_SCHEMA = {
    "type": "object",
    "properties": {
        "docs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["key", "title", "summary"],
            },
        },
    },
    "required": ["docs"],
}


def doc_tools() -> dict[str, str]:
    """The read tools for linked documents: {notion: fetch tool, google: Drive read tool}."""
    from laya.claude_code import discover

    tools: dict[str, str] = {}
    found = discover.cached()
    if found is not None:
        notion = found.platforms.get("notion")
        gmail = found.platforms.get("gmail")
        if notion and notion.tools.get("fetch"):
            tools["notion"] = notion.tools["fetch"]
        if gmail and gmail.tools.get("drive_read"):
            tools["google"] = gmail.tools["drive_read"]
    return tools


async def _stored_doc_tools() -> dict[str, str]:
    tools = doc_tools()
    if "notion" not in tools:
        notion = await store.get_source("notion")
        if notion and (notion.get("tools") or {}).get("fetch"):
            tools["notion"] = notion["tools"]["fetch"]
    if "google" not in tools:
        gmail = await store.get_source("gmail")
        if gmail and (gmail.get("tools") or {}).get("drive_read"):
            tools["google"] = gmail["tools"]["drive_read"]
    return tools


def build_prompt(links: list[dict[str, str]], tools: dict[str, str], context: str) -> str:
    lines = []
    for link in links:
        if link["kind"] == "notion":
            lines.append(f"- key={link['key']}: {tools['notion']} with id \"{link['id']}\"")
        else:
            lines.append(f"- key={link['key']}: {tools['google']} with fileId \"{link['id']}\"")
    listed = "\n".join(lines)
    return f"""Read these linked documents, one tool call each, and nothing else:
{listed}
For each return key, its title, and summary: 2-4 plain sentences on what the document says
that matters for the message below (decisions, asks, status, numbers), or the most relevant
excerpt. If a document cannot be read, return summary "".
The message that links them (for relevance only, do not summarize it):
{context[:1500]}"""


async def read_documents(
    links: list[dict[str, str]], context: str, source: dict[str, Any], tools: dict[str, str],
) -> dict[str, tuple[str, str]]:
    """Notion pages / Google files of one event in one model turn: {key: (title, summary)}."""
    readable = [link for link in links if link["kind"] in tools]
    if not readable:
        return {}
    config = source.get("config") or {}
    out = await cli.run_collect(
        "links", build_prompt(readable, tools, context), _SCHEMA,
        sorted({tools[link["kind"]] for link in readable}),
        model=config.get("model") or "haiku", deny=source.get("deny_tools") or [], timeout=300,
    )
    wanted = {link["key"] for link in readable}
    return {
        d["key"]: ((d.get("title") or "")[:200], (d.get("summary") or "")[:SUMMARY_CHARS])
        for d in out.get("docs") or [] if d.get("key") in wanted
    }


# ── enricher ─────────────────────────────────────────────────────────────


def section(entries: list[tuple[dict[str, str], dict[str, Any]]]) -> str:
    lines = [
        f"- {entry.get('title') or link['url']} ({link['url']}): {entry['summary']}"
        for link, entry in entries if entry.get("summary")
    ]
    return f"\n\n{SECTION}\n" + "\n".join(lines) if lines else ""


async def enrich_event(event: LayaEvent, source: dict[str, Any], *, model_allowed: bool) -> LayaEvent:
    body = event.content.body or ""
    if SECTION in body:
        return event
    links = extract_links(body, skip=event.subject.url)[:MAX_LINKS]
    if not links:
        return event
    entries = await cached([link["key"] for link in links])
    for link in links:
        if link["key"] in entries or link["kind"] != "github":
            continue
        try:
            title, summary = await read_github(link)
        except cli.ClaudeCodeError as e:
            log.info("claude_code_link_unreadable", link=link["url"], error=str(e)[:200])
            title, summary = "", ""
        entries[link["key"]] = await remember(link["key"], title, summary)

    pending = [link for link in links if link["key"] not in entries]
    if pending and model_allowed:
        tools = await _stored_doc_tools()
        try:
            results = await read_documents(pending, body, source, tools)
        except cli.ClaudeCodeError as e:
            log.warning("claude_code_links_failed", event_id=event.event_id, error=str(e)[:200])
            results = None
        if results is not None:
            for link in pending:
                if link["kind"] not in tools:
                    continue
                title, summary = results.get(link["key"], ("", ""))
                entries[link["key"]] = await remember(link["key"], title, summary)

    text = section([(link, entries[link["key"]]) for link in links if link["key"] in entries])
    if text:
        event.content.body = body + text
        event.content.metadata = {**event.content.metadata, "linked_documents": [
            link["url"] for link in links if (entries.get(link["key"]) or {}).get("summary")
        ]}
    return event


@register_enricher
async def linked_documents(events: list[LayaEvent], source: dict[str, Any]) -> list[LayaEvent]:
    """Append the "Linked documents" section to the events of one sweep."""
    from laya.pipeline.agent_budget import is_agent_budget_paused

    if source.get("platform") not in PLATFORMS:
        return events
    if not (source.get("config") or {}).get("linked_documents", True):
        return events
    model_allowed = not await is_agent_budget_paused()
    out = []
    for event in events:
        try:
            event = await enrich_event(event, source, model_allowed=model_allowed)
        except Exception as e:  # one event's links must not cost the others theirs
            log.warning("claude_code_link_event_failed", event_id=event.event_id, error=str(e)[:200])
        out.append(event)
    return out
