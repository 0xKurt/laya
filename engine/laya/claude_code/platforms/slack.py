# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Slack collector: DMs, mentions, the user's own messages and the threads the
user wrote in, one event per conversation and sweep.

A conversation is a DM / group DM (the channel) or a channel thread (channel
plus root ts; a root message is its own thread). Per conversation the sweep
yields the user's latest own message (it closes what came before) and one
event carrying every message after it, with up to `context_messages` earlier
messages as context, so a card reads on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from laya.claude_code import store
from laya.claude_code.platforms.common import (
    dedupe,
    local_tz,
    make_event,
    parse_ts,
    run_turn,
    split_conversation,
)
from laya.models.event import LayaEvent

PLATFORM = "slack"

# Most recently active threads of the user that are re-read per sweep.
THREADS_READ_PER_SWEEP = 15

SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "channel_name": {"type": ["string", "null"]},
                    # Free text: search results do not always say public vs
                    # private, and a strict enum made the model refuse the
                    # whole answer. normalize_message maps it.
                    "channel_type": {"type": "string", "description": "dm, group_dm, public or private"},
                    "ts": {"type": "string"},
                    "thread_ts": {"type": ["string", "null"]},
                    "user_id": {"type": "string"},
                    "user_name": {"type": "string"},
                    "text": {"type": "string"},
                    "permalink": {"type": "string"},
                    "is_context": {"type": "boolean"},
                },
                "required": ["channel_id", "ts", "user_id", "text", "permalink"],
            },
        },
    },
    "required": ["messages"],
}


def build_prompt(
    tools: dict[str, str], me: str, since: datetime, my_threads: list[tuple[str, str]],
    context_messages: int,
) -> str:
    after = int(since.timestamp())
    search, read_thread, read_channel = tools["search"], tools["read_thread"], tools["read_channel"]
    thread_step = ""
    if my_threads:
        listed = "\n".join(f"   - channel_id={c} thread_ts={t}" for c, t in my_threads)
        thread_step = (
            f"\n4. Read each of these threads with {read_thread} (oldest=\"{after}\") and take "
            f"its messages posted after unix time {after}:\n{listed}"
        )
    return f"""You are a data collector. Copy data exactly; do not summarize or judge.
My Slack user id is {me}. Collect Slack messages posted after unix time {after} using
{search} (pass after="{after}", sort="timestamp", include_bots=false,
include_context=false, response_format="detailed", limit=20; follow the cursor up to
3 pages per query). Run exactly these queries:
1. All direct and group DMs: channel_types="im,mpim", filters="".
2. Messages that mention me: filters="<@{me}>".
3. My own messages anywhere: filters="from:<@{me}>".{thread_step}
Do not add messages from any other search. Deduplicate by channel_id + ts.
Context: for every DM, group DM or thread in which someone other than me posted one of
the messages above, also read the up to {context_messages} messages posted right before
the earliest of them (DMs: {read_channel} with latest set to that ts; threads:
{read_thread}, including the thread's first message) and return them with
is_context=true. All other messages have is_context=false.
For each message return channel_id, channel_name, channel_type (dm, group_dm, public or
private), ts, thread_ts (the thread root ts, or null if not in a thread), user_id,
user_name, the full message text, its permalink and is_context.
Return {{"messages": []}} if there is nothing."""


def normalize_message(m: dict[str, Any]) -> dict[str, Any]:
    """Map whatever channel type the model reported onto dm/group_dm/public/private."""
    raw = (m.get("channel_type") or "").lower().replace("-", "_").replace(" ", "_")
    channel = str(m.get("channel_id") or "")
    if raw in ("dm", "im", "direct_message", "direct") or channel.startswith("D"):
        kind = "dm"
    elif raw in ("group_dm", "mpim", "group", "multi_person_dm", "mpdm"):
        kind = "group_dm"
    elif raw in ("private", "private_channel", "group_channel"):
        kind = "private"
    else:
        kind = "public"
    thread_ts = m.get("thread_ts") or None
    # Slack reports a root message's own ts as its thread_ts.
    return {**m, "channel_type": kind, "thread_ts": thread_ts}


def conversation_of(m: dict[str, Any]) -> str:
    """subject.id: a DM is one conversation per channel; elsewhere one per thread."""
    if m.get("channel_type") in ("dm", "group_dm"):
        return m["channel_id"]
    return f"{m['channel_id']}:{m.get('thread_ts') or m['ts']}"


def is_relevant(m: dict[str, Any], me: str, my_threads: set[str]) -> bool:
    """DMs, mentions, the user's own messages and threads the user wrote in.

    Applied in code to whatever the turn returns, so a model that collects
    too widely cannot put other people's channel chatter into the feed.
    """
    return (
        m.get("channel_type") in ("dm", "group_dm")
        or m.get("user_id") == me
        or f"<@{me}>" in (m.get("text") or "")
        or conversation_of(m) in my_threads
    )


def _line(m: dict[str, Any], me: str, tz) -> str:
    who = "Me" if m.get("user_id") == me else (m.get("user_name") or m.get("user_id"))
    when = parse_ts(m["ts"]).astimezone(tz).strftime("%a %d.%m %H:%M")
    return f"[{when}] {who}: {m.get('text', '')}"


def _body(msgs: list[dict], earlier: list[dict], me: str, tz) -> str:
    new = "\n".join(_line(m, me, tz) for m in msgs)
    if not earlier:
        return new
    return (
        "Earlier in the conversation:\n"
        + "\n".join(_line(m, me, tz) for m in earlier)
        + "\n\nNew:\n" + new
    )


def _event(
    conversation: str, msgs: list[dict], me: str, dm_partner: dict[str, str], tz,
    earlier: list[dict] | None = None,
) -> LayaEvent:
    first, last = msgs[0], msgs[-1]
    channel_type = last.get("channel_type", "public")
    channel_id = last["channel_id"]
    name = (last.get("channel_name") or channel_id).lstrip("#")
    if channel_type == "dm":
        partner = dm_partner.get(channel_id) or (name if name != channel_id else "")
        title = f"DM with {partner or 'direct message'}"
    elif channel_type == "group_dm":
        title = f"Group DM {name}" if name != channel_id else "Group DM"
    else:
        title = f"#{name}"
    thread_ts = last.get("thread_ts")
    return make_event(
        platform=PLATFORM,
        native_id=f"{channel_id}:{last['ts']}",
        timestamp=parse_ts(last["ts"]),
        raw_event_type="thread_reply" if thread_ts and thread_ts != last["ts"] else "message_received",
        actor_name=last.get("user_name") or "",
        actor_email="",
        actor_handle=last.get("user_id"),
        subject_type="thread",
        subject_id=conversation,
        subject_title=title,
        # The first message the user has not answered is where they pick up.
        subject_url=first.get("permalink"),
        body=_body(msgs, earlier or [], me, tz),
        metadata={
            "slack_channel_id": channel_id,
            "slack_channel_name": name if channel_type not in ("dm", "group_dm") else None,
            "slack_channel_type": channel_type,
            "slack_thread_ts": thread_ts or last["ts"],
            "slack_is_mention": any(f"<@{me}>" in (m.get("text") or "") for m in msgs),
            "slack_message_count": len(msgs),
        },
    )


def build_events(
    messages: list[dict[str, Any]], me: str, *, context_messages: int = 8, tz=timezone.utc,
) -> list[LayaEvent]:
    """Group normalized messages into conversation events."""
    messages = dedupe(messages, key=lambda m: (m["channel_id"], m["ts"], bool(m.get("is_context"))))
    # A DM is titled after the other person, also on the user's own messages in it.
    dm_partner = {
        m["channel_id"]: m["user_name"]
        for m in messages
        if m.get("channel_type") == "dm" and m.get("user_id") != me and m.get("user_name")
    }
    new: dict[str, list[dict]] = {}
    context: dict[str, list[dict]] = {}
    for m in messages:
        (context if m.get("is_context") else new).setdefault(conversation_of(m), []).append(m)

    events: list[LayaEvent] = []
    for conversation, msgs in new.items():
        msgs.sort(key=lambda m: float(m["ts"]))
        mine, unanswered, before = split_conversation(msgs, lambda m: m.get("user_id") == me)
        if mine is not None:
            events.append(_event(conversation, [mine], me, dm_partner, tz))
        if unanswered:
            new_ts = {m["ts"] for m in msgs}
            earlier = sorted(
                (m for m in context.get(conversation, []) if m["ts"] not in new_ts),
                key=lambda m: float(m["ts"]),
            ) + before
            earlier = earlier[-context_messages:] if context_messages > 0 else []
            events.append(_event(conversation, unanswered, me, dm_partner, tz, earlier=earlier))
    return events


async def _my_threads(memory_days: int) -> dict[str, str]:
    state = await store.get_state(PLATFORM, "thread:")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=memory_days)).isoformat()
    expired = [k for k, v in state.items() if v < cutoff]
    await store.delete_state(PLATFORM, expired)
    return {k[len("thread:"):]: v for k, v in state.items() if v >= cutoff}


async def collect(source: dict[str, Any], since: datetime) -> list[LayaEvent]:
    config = source.get("config") or {}
    me = (source.get("identity") or {}).get("slack_user_id")
    if not me:
        raise RuntimeError("Slack user id unknown; reconnect Slack")
    known = await _my_threads(int(config.get("thread_memory_days", 14)))
    recent = sorted(known.items(), key=lambda kv: kv[1], reverse=True)[:THREADS_READ_PER_SWEEP]
    to_read = [tuple(key.split(":", 1)) for key, _ in recent]
    context_messages = int(config.get("context_messages", 8))

    out = await run_turn(
        source, PLATFORM,
        build_prompt(source["tools"], me, since, to_read, context_messages),
        SCHEMA,
    )
    messages = [normalize_message(m) for m in out.get("messages", []) if m.get("channel_id") and m.get("ts")]

    # Channel threads the user writes in are remembered; replies there count
    # as relevant for `thread_memory_days`.
    now = datetime.now(timezone.utc).isoformat()
    for m in messages:
        if m.get("user_id") == me and not m.get("is_context") and m["channel_type"] not in ("dm", "group_dm"):
            conversation = conversation_of(m)
            known[conversation] = now
            await store.set_state(PLATFORM, f"thread:{conversation}", now)

    new = [m for m in messages if not m.get("is_context")]
    if config.get("only_relevant", True):
        new = [m for m in new if is_relevant(m, me, set(known))]
    kept = {conversation_of(m) for m in new}
    context = [m for m in messages if m.get("is_context") and conversation_of(m) in kept]
    return build_events(new + context, me, context_messages=context_messages, tz=local_tz(config))
