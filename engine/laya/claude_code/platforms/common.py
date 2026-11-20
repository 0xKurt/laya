# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Helpers shared by the platform collectors.

Event conventions (docs/claude-code-connector.md, "Event conventions"):
- `event_id` is a uuid5 of the platform's own id, so overlapping sweeps
  re-deliver the same ids and `POST /events` drops the duplicates;
- `source.connection_id` is `claude_code:<platform>`, which maps the event to
  the connector's `sources` row;
- `subject.id` is the conversation, so `event.entity_id` groups a
  conversation's events into one work item;
- `content.metadata.claude_code` is always true.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone, tzinfo
from typing import Any, TypeVar

import structlog

from laya.models.event import LayaEvent
from laya.tz import safe_zoneinfo

log = structlog.get_logger()

T = TypeVar("T")

# Optional steps run over every collector's events before they are posted,
# e.g. linked-document summaries. Each takes (events, source) and returns the
# events to post.
Enricher = Callable[[list[LayaEvent], dict[str, Any]], Awaitable[list[LayaEvent]]]
ENRICHERS: list[Enricher] = []


def register_enricher(fn: Enricher) -> Enricher:
    if fn not in ENRICHERS:
        ENRICHERS.append(fn)
    return fn


async def enrich(events: list[LayaEvent], source: dict[str, Any]) -> list[LayaEvent]:
    """Run the registered enrichers; a failing enricher leaves the events as they are."""
    for fn in ENRICHERS:
        try:
            events = await fn(events, source)
        except Exception as e:  # an enrichment must never cost the sweep its events
            log.warning("claude_code_enrich_failed", enricher=getattr(fn, "__name__", "?"), error=str(e))
    return events


def connection_id(platform: str) -> str:
    return f"claude_code:{platform}"


def event_id(platform: str, native_id: str) -> str:
    return "evt_" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"laya-claude-code:{platform}:{native_id}"))


def parse_ts(value: Any) -> datetime:
    """Accept ISO strings, Slack `1700000000.123456` timestamps or epoch numbers."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value or "").strip()
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local_tz(config: dict[str, Any]) -> tzinfo:
    name = (config or {}).get("timezone") or ""
    if name:
        return safe_zoneinfo(name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def make_event(
    *,
    platform: str,
    native_id: str,
    timestamp: datetime,
    raw_event_type: str,
    actor_name: str,
    actor_email: str,
    actor_handle: str | None,
    subject_type: str,
    subject_id: str,
    subject_title: str,
    subject_url: str | None,
    body: str,
    metadata: dict[str, Any],
) -> LayaEvent:
    return LayaEvent.model_validate({
        "event_id": event_id(platform, native_id),
        "timestamp": timestamp.isoformat(),
        "source": {
            "platform": platform,
            "connection_id": connection_id(platform),
            "raw_event_type": raw_event_type,
        },
        "actor": {"name": actor_name or "", "email": (actor_email or "").lower(),
                  "platform_handle": actor_handle or None},
        "subject": {"type": subject_type, "id": subject_id, "title": (subject_title or "")[:300],
                    "url": subject_url or None},
        "content": {"body": body, "attachments": [], "metadata": {**metadata, "claude_code": True}},
    })


def split_conversation(
    msgs: list[T], is_mine: Callable[[T], bool]
) -> tuple[T | None, list[T], list[T]]:
    """Split a conversation's new messages (oldest first) at the user's last one.

    Returns (the user's last message or None, the messages after it, the
    messages up to and including it). The user's message closes what came
    before; the messages after it are still waiting on the user.
    """
    own = [i for i, m in enumerate(msgs) if is_mine(m)]
    if not own:
        return None, list(msgs), []
    last = own[-1]
    return msgs[last], list(msgs[last + 1:]), list(msgs[: last + 1])


def my_emails(source: dict[str, Any]) -> set[str]:
    """The user's email addresses: the platform identity plus team.json `self`."""
    from laya.config import get_self_user

    emails = {(source.get("identity") or {}).get("email") or ""}
    self_user = get_self_user() or {}
    emails.update(self_user.get("emails") or [])
    return {e.strip().lower() for e in emails if e and e.strip()}


def my_name(source: dict[str, Any]) -> str:
    from laya.config import get_self_user

    return (
        (get_self_user() or {}).get("name")
        or (source.get("identity") or {}).get("name")
        or "me"
    )


def dedupe(items: Iterable[T], key: Callable[[T], Any]) -> list[T]:
    seen: set = set()
    out: list[T] = []
    for it in items:
        k = key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def read_tools(source: dict[str, Any], platform: str) -> list[str]:
    """The discovered read tools of a platform (never its send tool)."""
    from laya.claude_code.discover import READ_ROLES

    tools = source.get("tools") or {}
    return [tools[r] for r in READ_ROLES.get(platform, ()) if tools.get(r)]


async def run_turn(source: dict[str, Any], platform: str, prompt: str, schema: dict) -> dict[str, Any]:
    from laya.claude_code import cli

    config = source.get("config") or {}
    return await cli.run_collect(
        platform, prompt, schema, read_tools(source, platform),
        model=config.get("model") or cli.AUTOMATIC_MODEL,
        deny=source.get("deny_tools") or [],
        env=source.get("account_env") or None,
        account_id=source.get("account_id"),
        agent_id=source.get("agent_id"),
    )
