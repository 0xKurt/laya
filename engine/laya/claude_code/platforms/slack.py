# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Slack collector: DMs, mentions, the user's own messages and the threads the
user wrote in, one event per conversation and sweep.

A conversation is a DM / group DM (the channel) or a channel thread (channel
plus root ts; a root message is its own thread). Per conversation the sweep
yields the user's latest own message (it closes what came before) and one
event carrying every message after it, with up to `context_messages` earlier
messages as context, so a card reads on its own.

A sweep is at most two short `claude -p` turns:

1. the search turn: three searches (`with:` me, `from:` me, mentions of me),
   results copied in compact field names, no thread or channel reads;
2. the context turn, only when needed: the earlier messages of conversations
   with a new message from someone else that no earlier sweep delivered
   (`seen:` state), at most `CONTEXT_CONVERSATIONS` of them.

Grouping, relevance, titles and permalinks are computed here, not asked of
the model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from laya.claude_code import store
from laya.claude_code.platforms.common import (
    dedupe,
    local_tz,
    make_event,
    my_name,
    parse_ts,
    run_turn,
    split_conversation,
)
from laya.models.event import LayaEvent

PLATFORM = "slack"

# Hard caps of one sweep, so an hourly sweep stays a few short tool calls.
SEARCH_LIMIT = 20  # results per search page (the tool's maximum)
SEARCH_PAGES = 2  # pages per query
TEXT_CHARS = 1500  # characters kept of one new message
OWN_TEXT_CHARS = 500  # of the user's own message (it closes the conversation)
CONTEXT_TEXT_CHARS = 600  # of an earlier message read as context
CONTEXT_CONVERSATIONS = 8  # conversations whose earlier messages are read per sweep
THREAD_READ_LIMIT = 50  # replies a context read of a thread may return

# Compact field names keep the model's output (every message is copied once)
# short; `expand_message` maps them onto the names the rest of the module uses.
_MESSAGE = {
    "type": "object",
    "properties": {
        "c": {"type": "string", "description": "channel id"},
        "ts": {"type": "string", "description": "Message_ts"},
        "th": {"type": ["string", "null"], "description": "thread root ts, null if not a reply"},
        "u": {"type": "string", "description": "sender user id"},
        "n": {"type": "string", "description": "sender name"},
        "t": {"type": "string", "description": "message text"},
    },
    "required": ["c", "ts", "u", "t"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "ws": {"type": ["string", "null"], "description": "workspace subdomain of the permalinks"},
        "ch": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "nm": {"type": ["string", "null"], "description": "channel name"},
                    # Free text: search results do not always say public vs
                    # private, and a strict enum made the model refuse the
                    # whole answer. normalize_message maps it.
                    "ty": {"type": "string", "description": "dm, group_dm or channel"},
                    "pp": {"type": "array", "items": {"type": "string"},
                           "description": "participant names of a DM / group DM"},
                },
                "required": ["id", "ty"],
            },
        },
        "m": {
            "type": "array",
            "items": {
                **_MESSAGE,
                "properties": {**_MESSAGE["properties"],
                               "q": {"type": "string", "description": "query letter w, o or m"}},
            },
        },
    },
    "required": ["m"],
}

CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {"m": {"type": "array", "items": _MESSAGE}},
    "required": ["m"],
}


def build_prompt(tools: dict[str, str], me: str, since: datetime) -> str:
    """The search turn: three narrow searches, results copied in compact form."""
    after = int(since.timestamp())
    return f"""You are a data collector. Copy values exactly; never summarize, never add messages.
My Slack user id is {me}. In ONE step, call {tools['search']} three times in parallel, each with
after="{after}", sort="timestamp", response_format="detailed", include_context=false,
include_bots=false, limit={SEARCH_LIMIT}, natural_language_query="" and:
  w: filters="with:<@{me}>"                                 (DMs, group DMs, threads I am in)
  m: keywords=["<@{me}>"], filters="-from:<@{me}>"           (mentions of me)
  o: filters="from:<@{me}>"                                  (my own messages)
Only a query that returned a full page of {SEARCH_LIMIT} gets its next page (cursor), once.
Call no other tool. Return in "m" every result, except that of my own messages (sender {me})
only the newest per conversation (channel + thread) is returned; the same c+ts only once.
Fields: q = letter of the first query that found it,
c = channel ID, ts = Message_ts, th = the thread_ts parameter of its permalink (null if none),
u = sender ID, n = sender name, t = the text (at most {TEXT_CHARS} characters; for o at most
{OWN_TEXT_CHARS}).
In "ch" list each channel once: id, nm (name without #, null for DMs), ty (dm, group_dm or
channel), pp (participant names, only for DMs and group DMs).
ws = the workspace subdomain of the permalinks (the part before .slack.com).
Return {{"m": []}} if there is nothing."""


def build_context_prompt(tools: dict[str, str], wanted: list[dict[str, Any]], count: int) -> str:
    """The context turn: the messages right before the new ones of each listed conversation."""
    lines = []
    for w in wanted:
        if w["thread_ts"]:
            lines.append(
                f"- {tools['read_thread']} channel_id={w['channel_id']} message_ts={w['thread_ts']} "
                f"latest={w['before']} limit={THREAD_READ_LIMIT}: return the first message and the "
                f"last {max(count - 1, 0)} messages before ts {w['before']}"
            )
        else:
            lines.append(
                f"- {tools['read_channel']} channel_id={w['channel_id']} latest={w['before']} "
                f"limit={count}: return the messages before ts {w['before']}"
            )
    listed = "\n".join(lines)
    return f"""You are a data collector. Copy values exactly; never summarize.
In ONE step make all of these tool calls in parallel (response_format="detailed"), then answer;
call no other tool:
{listed}
For each returned message give c = channel ID, ts = its ts, th = thread root ts (null outside
threads), u = sender ID, n = sender name, t = the text (at most {CONTEXT_TEXT_CHARS} characters).
Return {{"m": []}} if there is nothing."""


def permalink(workspace: str | None, channel: str, ts: str, thread_ts: str | None) -> str | None:
    if not workspace or not channel or not ts:
        return None
    url = f"https://{workspace}.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


def expand_message(
    m: dict[str, Any], channels: dict[str, dict[str, Any]], workspace: str | None, *, context: bool,
) -> dict[str, Any]:
    """A compact turn message in the module's own field names (not yet normalized)."""
    channel = str(m.get("c") or "")
    info = channels.get(channel) or {}
    ts = normalize_ts(m.get("ts"))
    thread_ts = normalize_ts(m.get("th") or None)
    return {
        "channel_id": channel,
        "channel_name": info.get("nm"),
        "channel_type": info.get("ty") or "",
        "participants": list(info.get("pp") or []),
        "ts": ts,
        "thread_ts": thread_ts,
        "user_id": m.get("u") or "",
        "user_name": m.get("n") or "",
        "text": (m.get("t") or "")[:CONTEXT_TEXT_CHARS if context else
                                      OWN_TEXT_CHARS if m.get("q") == "o" else TEXT_CHARS],
        "permalink": permalink(workspace, channel, ts or "", thread_ts),
        "is_context": context,
        "query": m.get("q"),
    }


def normalize_ts(ts: Any) -> str | None:
    """Slack's `1700000000.123456` form.

    Permalinks carry the ts without its dot (`p1700000000123456`), and a
    model copying from them returns that form, which would otherwise parse as
    a timestamp far in the future and split one message into two ids.
    """
    if ts is None:
        return None
    text = str(ts).strip()
    if text.startswith("p") and text[1:].isdigit():
        text = text[1:]
    if text.isdigit() and len(text) > 10:
        text = f"{text[:10]}.{text[10:]}"
    return text or None


def normalize_message(m: dict[str, Any]) -> dict[str, Any]:
    """Map whatever channel type the model reported onto dm/group_dm/public/private,
    and bring timestamps into Slack's dotted form."""
    raw = (m.get("channel_type") or "").lower().replace("-", "_").replace(" ", "_")
    channel = str(m.get("channel_id") or "")
    # Group DMs can have a `D` channel id like a 1:1 DM, so the reported type
    # and the participant count decide before the id prefix does.
    if raw in ("group_dm", "mpim", "group", "multi_person_dm", "mpdm") or len(m.get("participants") or []) > 2:
        kind = "group_dm"
    elif raw in ("dm", "im", "direct_message", "direct") or channel.startswith("D"):
        kind = "dm"
    elif raw in ("private", "private_channel", "group_channel"):
        kind = "private"
    else:
        kind = "public"
    return {**m, "channel_type": kind, "ts": normalize_ts(m.get("ts")),
            "thread_ts": normalize_ts(m.get("thread_ts") or None)}


def conversation_of(m: dict[str, Any]) -> str:
    """subject.id: a DM is one conversation per channel; elsewhere one per thread."""
    if m.get("channel_type") in ("dm", "group_dm"):
        return m["channel_id"]
    return f"{m['channel_id']}:{m.get('thread_ts') or m['ts']}"


def mentions(m: dict[str, Any], me: str) -> bool:
    """The message mentions the user: `<@U…>` or `<@U…|name>`."""
    text = m.get("text") or ""
    return f"<@{me}>" in text or f"<@{me}|" in text


def is_relevant(m: dict[str, Any], me: str, my_threads: set[str]) -> bool:
    """DMs, mentions, the user's own messages and threads the user wrote in.

    Applied in code to whatever the turn returns, so a model that collects
    too widely cannot put other people's channel chatter into the feed. A
    thread reply found by the `with:` query (threads the user is in) counts
    as a thread the user wrote in.
    """
    return (
        m.get("channel_type") in ("dm", "group_dm")
        or m.get("user_id") == me
        or mentions(m, me)
        or conversation_of(m) in my_threads
        or (m.get("query") == "w" and bool(m.get("thread_ts")) and m.get("thread_ts") != m.get("ts"))
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
    earlier: list[dict] | None = None, group_members: dict[str, list[str]] | None = None,
) -> LayaEvent:
    first, last = msgs[0], msgs[-1]
    group_members = group_members or {}
    channel_type = last.get("channel_type", "public")
    channel_id = last["channel_id"]
    name = (last.get("channel_name") or channel_id).lstrip("#")
    if channel_type == "dm":
        partner = (
            dm_partner.get(channel_id) or ", ".join(group_members.get(channel_id) or [])
            or (name if name != channel_id else "")
        )
        title = f"DM with {partner or 'direct message'}"
    elif channel_type == "group_dm":
        others = group_members.get(channel_id) or []
        title = f"Group DM: {', '.join(others)}" if others else "Group DM"
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
            # The key Slack egress (egress/platforms/slack.py) reads the channel from.
            "slack_channel": channel_id,
            "slack_channel_name": name if channel_type not in ("dm", "group_dm") else None,
            "slack_channel_type": channel_type,
            "slack_thread_ts": thread_ts or last["ts"],
            "slack_is_mention": any(mentions(m, me) for m in msgs),
            "slack_message_count": len(msgs),
        },
    )


def dm_members(
    messages: list[dict[str, Any]], me: str, my_names: set[str] | None = None,
) -> dict[str, list[str]]:
    """Per DM / group DM the names of the other members, without the user's own name."""
    own = {n.strip().lower() for n in (my_names or set()) if n}
    own |= {(m.get("user_name") or "").strip().lower() for m in messages if m.get("user_id") == me}
    own.discard("")
    members: dict[str, list[str]] = {}
    for m in messages:
        if m.get("channel_type") not in ("dm", "group_dm"):
            continue
        names = members.setdefault(m["channel_id"], [])
        # The participant list has everyone under one spelling; senders' names
        # only fill in when the turn reported no participants.
        candidates = list(m.get("participants") or [])
        if not candidates and m.get("user_id") != me:
            candidates.append(m.get("user_name") or "")
        for name in candidates:
            name = name.strip()
            if name and name.lower() not in own and name not in names:
                names.append(name)
    return members


def build_events(
    messages: list[dict[str, Any]], me: str, *, context_messages: int = 8, tz=timezone.utc,
    my_names: set[str] | None = None,
) -> list[LayaEvent]:
    """Group normalized messages into conversation events."""
    messages = dedupe(messages, key=lambda m: (m["channel_id"], m["ts"], bool(m.get("is_context"))))
    # A DM is titled after the other person, also on the user's own messages in it.
    dm_partner = {
        m["channel_id"]: m["user_name"]
        for m in messages
        if m.get("channel_type") == "dm" and m.get("user_id") != me and m.get("user_name")
    }
    groups = dm_members(messages, me, my_names)
    new: dict[str, list[dict]] = {}
    context: dict[str, list[dict]] = {}
    for m in messages:
        (context if m.get("is_context") else new).setdefault(conversation_of(m), []).append(m)

    events: list[LayaEvent] = []
    for conversation, msgs in new.items():
        msgs.sort(key=lambda m: float(m["ts"]))
        mine, unanswered, before = split_conversation(msgs, lambda m: m.get("user_id") == me)
        if mine is not None:
            events.append(_event(conversation, [mine], me, dm_partner, tz, group_members=groups))
        if unanswered:
            new_ts = {m["ts"] for m in msgs}
            earlier = sorted(
                (m for m in context.get(conversation, []) if m["ts"] not in new_ts),
                key=lambda m: float(m["ts"]),
            ) + before
            earlier = earlier[-context_messages:] if context_messages > 0 else []
            events.append(_event(conversation, unanswered, me, dm_partner, tz, earlier=earlier, group_members=groups))
    return events


async def _memory(prefix: str, memory_days: int) -> dict[str, str]:
    """Remembered conversations under a state key prefix; expired ones are dropped."""
    state = await store.get_state(PLATFORM, prefix)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=memory_days)).isoformat()
    expired = [k for k, v in state.items() if v < cutoff]
    await store.delete_state(PLATFORM, expired)
    return {k[len(prefix):]: v for k, v in state.items() if v >= cutoff}


def context_wanted(
    new: list[dict[str, Any]], me: str, seen: set[str], limit: int = CONTEXT_CONVERSATIONS,
) -> list[dict[str, Any]]:
    """Conversations whose earlier messages have to be read.

    Only conversations with a new message from someone else after the
    user's last one, that no earlier sweep delivered (an earlier sweep's
    event already carries what came before), and that have earlier messages
    at all: DMs and thread replies, not a channel root message. Newest first,
    at most `limit`.
    """
    by_conv: dict[str, list[dict]] = {}
    for m in new:
        by_conv.setdefault(conversation_of(m), []).append(m)
    wanted = []
    for conversation, msgs in by_conv.items():
        if conversation in seen:
            continue
        msgs.sort(key=lambda m: float(m["ts"]))
        _, unanswered, before = split_conversation(msgs, lambda m: m.get("user_id") == me)
        if not unanswered or before:
            continue
        first = msgs[0]
        is_dm = first.get("channel_type") in ("dm", "group_dm")
        thread_ts = first.get("thread_ts")
        if not is_dm and (not thread_ts or thread_ts == first["ts"]):
            continue
        wanted.append({
            "conversation": conversation,
            "channel_id": first["channel_id"],
            "thread_ts": None if is_dm else thread_ts,
            "before": first["ts"],
            "latest": float(msgs[-1]["ts"]),
        })
    wanted.sort(key=lambda w: w["latest"], reverse=True)
    return wanted[:limit]


async def collect(source: dict[str, Any], since: datetime) -> list[LayaEvent]:
    config = source.get("config") or {}
    me = (source.get("identity") or {}).get("slack_user_id")
    if not me:
        raise RuntimeError("Slack user id unknown; reconnect Slack")
    memory_days = int(config.get("thread_memory_days", 14))
    known = await _memory("thread:", memory_days)
    seen = await _memory("seen:", memory_days)
    context_messages = int(config.get("context_messages", 8))
    tools = source["tools"]

    out = await run_turn(source, PLATFORM, build_prompt(tools, me, since), SCHEMA)
    workspace = out.get("ws") or None
    channels = {c["id"]: c for c in out.get("ch") or [] if c.get("id")}
    messages = [
        normalize_message(expand_message(m, channels, workspace, context=False))
        for m in out.get("m") or [] if m.get("c") and m.get("ts")
    ]

    # Channel threads the user writes in are remembered; replies there count
    # as relevant for `thread_memory_days`.
    now = datetime.now(timezone.utc).isoformat()
    for m in messages:
        if m.get("user_id") == me and m["channel_type"] not in ("dm", "group_dm"):
            conversation = conversation_of(m)
            known[conversation] = now
            await store.set_state(PLATFORM, f"thread:{conversation}", now)

    new = messages
    if config.get("only_relevant", True):
        new = [m for m in new if is_relevant(m, me, set(known))]

    context: list[dict[str, Any]] = []
    wanted = context_wanted(new, me, set(seen)) if context_messages > 0 else []
    if wanted:
        ctx_out = await run_turn(
            source, PLATFORM, build_context_prompt(tools, wanted, context_messages), CONTEXT_SCHEMA,
        )
        wanted_ids = {w["conversation"] for w in wanted}
        for m in ctx_out.get("m") or []:
            if not (m.get("c") and m.get("ts")):
                continue
            msg = normalize_message(expand_message(m, channels, workspace, context=True))
            if conversation_of(msg) in wanted_ids:
                context.append(msg)

    for conversation in {conversation_of(m) for m in new}:
        await store.set_state(PLATFORM, f"seen:{conversation}", now)

    my_names = {(source.get("identity") or {}).get("name") or "", my_name(source)}
    return build_events(
        new + context, me, context_messages=context_messages, tz=local_tz(config), my_names=my_names,
    )
