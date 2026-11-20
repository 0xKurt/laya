# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""My work refreshes update the related board cards.

My work refreshes GitHub every 10 minutes and Linear every 30, but a card
about the same pull request or issue only changed at the card re-check
(recheck.py, after `recheck_after_hours`) or the next sweep. After each My
work section refresh, and after each My work row action, this module diffs
the new rows against the previous ones and updates the cards that refer to
the same item:

- An item that concludes the work (a pull request merged or closed, a Linear
  issue moved to a completed or canceled state) closes its open cards through
  the conclusion path the re-check uses: a `resolved_in_source` event posted
  like a sweep's events, which `card_status.apply_conclusion` applies under
  the entity's status lock (status_set_by `laya`, reason "Resolved in the
  source: <url>", the late-card rule included).
  A pull request that left the list is checked with `gh` first: it may only
  have left the scope. On a GitHub refresh, open cards about other pull
  requests that are not in the list are checked the same way (at most
  `GH_VERIFY_LIMIT` per refresh, each at most once per `VERIFY_COOLDOWN`), so
  a merge missed while Laya was closed still closes its cards.
- Any other change (checks failing, changes requested, new review threads,
  conflicts, an issue that left the list, reassigned or reprioritised) runs
  the existing re-check of the related cards now instead of after
  `recheck_after_hours`: GitHub cards through `gh` (no tokens, at most
  `GH_RECHECK_LIMIT`), other cards through one model turn per platform (at
  most `MCP_RECHECK_LIMIT` entities, only while the agent budget is not
  paused and an eligible login exists).

Cards are matched by entity, `source_url`, `source_ref`, header and summary:
GitHub pull request URLs (any sub-page, e.g. `/files`) and Linear issue URLs
or identifiers. The re-check's rules apply: only `ready` /
`requires_approval` cards the user did not reopen, whose event is the latest
of their entity, from a connected Claude Code source. A card in another
conversation (a Slack thread naming the PR) is closed only when every open
card of that conversation refers to concluded items only; a card with
another open ask stays. Closed cards are announced by the existing
`card_updated` websocket message.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from laya.claude_code import recheck, store
from laya.claude_code.platforms.common import connection_id, parse_ts

log = structlog.get_logger()

STATE_PLATFORM = "my_work_related"
_VERIFY_PREFIX = "verified:"

GH_VERIFY_LIMIT = 10
GH_RECHECK_LIMIT = 10
MCP_RECHECK_LIMIT = 3
VERIFY_COOLDOWN = timedelta(hours=1)
# A changed item re-checks its cards at most this often.
RECHECK_MIN_GAP = timedelta(minutes=10)

_TERMINAL_LINEAR = ("completed", "canceled", "cancelled", "duplicate")
_NOT_LINEAR = {"ERC", "EIP", "UTF", "SHA", "ISO", "RFC", "CVE", "BIP", "SIP", "AES", "TLS", "HTTP",
               "X", "PR", "L1", "L2"}

_GH_PR_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pulls?/(\d+)", re.I)
_LINEAR_URL = re.compile(r"linear\.app/[^/\s]+/issue/([A-Za-z][A-Za-z0-9]{0,9}-\d+)", re.I)
_LINEAR_KEY = re.compile(r"(?<![A-Za-z0-9/#-])([A-Z][A-Z0-9]{0,9})-(\d{1,6})(?![0-9A-Za-z])")

_lock = asyncio.Lock()


# ── references ───────────────────────────────────────────────────────────


def gh_ref(repo: str, number: Any) -> str:
    return f"gh:{repo.lower()}#{int(number)}"


def linear_ref(identifier: str) -> str:
    team, _, n = identifier.strip().upper().partition("-")
    return f"lin:{team}-{int(n)}" if n.isdigit() else f"lin:{identifier.strip().upper()}"


def item_ref(section: str, key: str) -> str | None:
    """The reference of a My work row key (`owner/repo#N`, `TRAD-1`)."""
    if section == "github":
        repo, _, number = key.rpartition("#")
        return gh_ref(repo, number) if repo and number.isdigit() else None
    return linear_ref(key) if key else None


def entity_ref(entity_id: str) -> str | None:
    """The item an entity id is about, when it is a pull request or a Linear issue."""
    parts = (entity_id or "").split(":", 2)
    if len(parts) != 3:
        return None
    platform, subject_type, subject_id = parts
    if platform == "github" and subject_type == "pull_request":
        return item_ref("github", subject_id)
    if platform == "linear":
        return linear_ref(subject_id)
    return None


def text_refs(text: str | None) -> set[str]:
    """Pull request URLs (any sub-page) and Linear issue URLs or identifiers in a text."""
    text = text or ""
    refs = {gh_ref(f"{m.group(1)}/{m.group(2)}", m.group(3)) for m in _GH_PR_URL.finditer(text)}
    refs |= {linear_ref(m.group(1)) for m in _LINEAR_URL.finditer(text)}
    for m in _LINEAR_KEY.finditer(text):
        if m.group(1) not in _NOT_LINEAR and not m.group(1).isdigit():
            refs.add(linear_ref(f"{m.group(1)}-{m.group(2)}"))
    return refs


def card_refs(row: Any) -> set[str]:
    refs = set()
    own = entity_ref(row["entity_id"])
    if own:
        refs.add(own)
    for col in ("source_url", "source_ref", "header", "summary", "subject_url"):
        refs |= text_refs(row[col])
    return refs


# ── diffs ────────────────────────────────────────────────────────────────


@dataclass
class Change:
    """One My work item that changed."""

    ref: str
    url: str | None
    # "resolved": concludes the work; "gone": left the list, state unknown;
    # "changed": still open, something the cards may depend on changed.
    kind: str
    reasons: list[str] = field(default_factory=list)
    note: str | None = None
    at: datetime | None = None


def _row_url(section: str, row: dict[str, Any] | None) -> str | None:
    return (row or {}).get("url") or None


def github_reasons(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What changed on an open pull request that its cards may depend on."""
    reasons = []
    old_checks, new_checks = old.get("checks") or {}, new.get("checks") or {}
    if new_checks.get("state") != old_checks.get("state") and new_checks.get("state") in ("failure", "success"):
        reasons.append(f"checks {new_checks.get('state')}")
    elif (new_checks.get("failing") or 0) > (old_checks.get("failing") or 0):
        reasons.append("more failing checks")
    if (new.get("changes_requested") or 0) > (old.get("changes_requested") or 0):
        reasons.append("changes requested")
    if (new.get("approvals") or 0) > (old.get("approvals") or 0):
        reasons.append("approved")
    if (new.get("unresolved_threads") or 0) > (old.get("unresolved_threads") or 0):
        reasons.append("new review threads")
    if new.get("review_decision") != old.get("review_decision") and new.get("review_decision"):
        reasons.append(f"review {str(new.get('review_decision')).lower()}")
    if new.get("conflicts") and not old.get("conflicts"):
        reasons.append("merge conflicts")
    if old.get("draft") and not new.get("draft"):
        reasons.append("ready for review")
    return reasons


def linear_reasons(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    reasons = []
    if (new.get("state") or "") != (old.get("state") or ""):
        reasons.append(f"status {new.get('state')}")
    if new.get("assignee") != old.get("assignee") or new.get("assigned_to_me") != old.get("assigned_to_me"):
        reasons.append("reassigned")
    if new.get("priority") != old.get("priority"):
        reasons.append("priority changed")
    return reasons


def _linear_resolution(row: dict[str, Any] | None) -> tuple[str, str] | None:
    """(note, state type) when a Linear row is in a completed or canceled state."""
    state_type = ((row or {}).get("state_type") or "").lower()
    if state_type in _TERMINAL_LINEAR:
        return f"Linear status: {(row or {}).get('state') or state_type}.", state_type
    return None


def diff_row(section: str, key: str, old: dict[str, Any] | None, new: dict[str, Any] | None,
             final: dict[str, Any] | None = None) -> Change | None:
    """The change of one row; `final` is the item's last known state when `new` is None."""
    ref = item_ref(section, key)
    if ref is None:
        return None
    url = _row_url(section, new) or _row_url(section, final) or _row_url(section, old)
    if new is None:
        resolved = _linear_resolution(final) if section == "linear" else None
        if resolved:
            return Change(ref, url, "resolved", [resolved[1]], note=resolved[0],
                          at=datetime.now(timezone.utc))
        return Change(ref, url, "gone", ["left My work"]) if old is not None or final is not None else None
    if section == "linear":
        resolved = _linear_resolution(new)
        if resolved:
            return Change(ref, url, "resolved", [resolved[1]], note=resolved[0],
                          at=datetime.now(timezone.utc))
    if old is None:
        return None
    reasons = github_reasons(old, new) if section == "github" else linear_reasons(old, new)
    return Change(ref, url, "changed", reasons) if reasons else None


def diff_items(section: str, old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[Change]:
    """Changes between two refreshes of a section."""
    from laya.claude_code.my_work import item_key

    old_by = {item_key(section, r): r for r in old or []}
    new_by = {item_key(section, r): r for r in new or []}
    changes = []
    for key in old_by.keys() | new_by.keys():
        change = diff_row(section, key, old_by.get(key), new_by.get(key))
        if change:
            changes.append(change)
    return changes


# ── cards ────────────────────────────────────────────────────────────────


async def _connected_platforms() -> list[str]:
    return [s["platform"] for s in await store.list_sources()]


async def open_cards() -> dict[str, list[Any]]:
    """Open cards a re-check may conclude, by entity, from connected Claude Code sources."""
    platforms = await _connected_platforms()
    if not platforms:
        return {}
    marks = ", ".join("?" for _ in platforms)
    rows = await recheck.open_card_rows(f"AND e.source_connection_id IN ({marks})",
                                        tuple(connection_id(p) for p in platforms))
    by_entity: dict[str, list[Any]] = {}
    for row in rows:
        by_entity.setdefault(row["entity_id"], []).append(row)
    return by_entity


def _entity(rows: list[Any]) -> dict[str, Any]:
    """The entity of a group of open cards, with the latest card creation."""
    entity = recheck.entity_of(rows[-1])
    created = [recheck.entity_of(r)["created_at"] for r in rows]
    created = [c for c in created if c]
    if created:
        entity["created_at"] = max(created)
    return entity


async def _post(event) -> None:
    """Post an event through the same path as a sweep's events."""
    from laya.api.events import receive_event

    await receive_event(event)


async def conclude(resolved: dict[str, Change], cards: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Close the open cards about resolved items. Returns one record per closing event."""
    from laya.pipeline.card_status import conclusion_reason

    out = []
    for entity_id, rows in cards.items():
        refs_per_card = [card_refs(r) for r in rows]
        matched = set().union(*refs_per_card) & resolved.keys()
        if not matched:
            continue
        own = entity_ref(entity_id)
        entity = _entity(rows)
        if own in resolved:
            change = resolved[own]
            when = recheck.own_resolution_time(entity, change.at)
        else:
            open_asks = [r for r, refs in zip(rows, refs_per_card) if not refs or not refs <= resolved.keys()]
            if open_asks:
                log.info("claude_code_related_card_kept", entity_id=entity_id,
                         card_ids=[r["card_id"] for r in open_asks], reason="another open ask")
                continue
            change = max((resolved[m] for m in matched),
                         key=lambda c: c.at or datetime.min.replace(tzinfo=timezone.utc))
            when = change.at or datetime.now(timezone.utc)
            if entity["timestamp"] > when:
                log.info("claude_code_related_card_kept", entity_id=entity_id,
                         card_ids=[r["card_id"] for r in rows], reason="asked after the item was resolved")
                continue
        event = recheck.resolution_event(
            rows[-1]["source_platform"], entity, answered_at=when, url=change.url,
            by_user=None, note=change.note or "Resolved in the source.",
        )
        reason = conclusion_reason(event)
        await _post(event)
        for r in rows:
            log.info("claude_code_related_card_concluded", card_id=r["card_id"],
                     entity_id=entity_id, item=change.ref, reason=reason)
        out.append({"entity_id": entity_id, "card_ids": [r["card_id"] for r in rows],
                    "reason": reason, "event_id": event.event_id})
    return out


async def _verify_github(refs: dict[str, str | None]) -> dict[str, Change]:
    """Check pull requests with `gh`; returns the merged or closed ones."""
    resolved = {}
    for ref, url in refs.items():
        repo, _, number = ref[3:].rpartition("#")
        item = await recheck.github_item(repo, "pulls", number)
        if item is None:
            continue
        closed = recheck.github_closed(item)
        if closed:
            when, note = closed
            resolved[ref] = Change(ref, item.get("html_url") or url, "resolved", [note], note=note, at=when)
    return resolved


async def _due_for_verify(refs: list[str], now: datetime) -> list[str]:
    marks = await store.get_state(STATE_PLATFORM, _VERIFY_PREFIX)
    due = []
    for ref in refs:
        last = marks.get(_VERIFY_PREFIX + ref)
        if last and now - parse_ts(last) < VERIFY_COOLDOWN:
            continue
        due.append(ref)
    expired = [k for k, v in marks.items() if now - parse_ts(v) > timedelta(days=7)]
    await store.delete_state(STATE_PLATFORM, expired)
    return due


def _in_orgs(ref: str, orgs: list[str]) -> bool:
    if not orgs:
        return True
    owner = ref[3:].split("/", 1)[0]
    return owner in {o.lower() for o in orgs if o}


async def _recheck_now(changed: dict[str, Change], cards: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Run the existing re-check now for the open cards about changed items."""
    from laya.claude_code import service

    now = datetime.now(timezone.utc)
    by_platform: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for entity_id, rows in cards.items():
        refs = set().union(*(card_refs(r) for r in rows)) & changed.keys()
        if refs:
            platform = rows[-1]["source_platform"]
            by_platform.setdefault(platform, []).append((_entity(rows), sorted(refs)[0]))
    done = []
    for platform, items in by_platform.items():
        if platform not in recheck._RECHECKABLE:
            continue
        marks = await store.get_state(platform, recheck._STATE_PREFIX)
        items = [(e, ref) for e, ref in items
                 if not (marks.get(recheck._STATE_PREFIX + e["entity_id"])
                         and now - parse_ts(marks[recheck._STATE_PREFIX + e["entity_id"]]) < RECHECK_MIN_GAP)]
        limit = GH_RECHECK_LIMIT if platform == "github" else MCP_RECHECK_LIMIT
        items = items[:limit]
        if not items:
            continue
        reason = await service.blocked_reason(platform)
        if reason:
            log.info("claude_code_related_recheck_skipped", platform=platform, reason=reason)
            continue
        source = await store.get_source(platform)
        if source is None:
            continue
        if platform != "github":
            fields = await service._assign_account(platform)
            if fields is None:
                log.info("claude_code_related_recheck_skipped", platform=platform, reason=service.NO_ACCOUNT_ERROR)
                continue
            source = {**source, **fields}
        entities = [e for e, _ in items]
        try:
            events = await recheck.check_entities(platform, source, entities)
        except Exception as e:  # a failed re-check leaves the cards as they are
            log.warning("claude_code_related_recheck_failed", platform=platform, error=str(e))
            continue
        for event in events:
            await _post(event)
        for e, ref in items:
            log.info("claude_code_related_recheck", platform=platform, entity_id=e["entity_id"],
                     item=ref, reasons=changed[ref].reasons)
        done.append({"platform": platform, "checked": len(entities), "resolved": len(events)})
    return done


async def apply(section: str, changes: list[Change], *, open_refs: set[str] | None = None,
                config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Update the cards related to `changes`.

    `open_refs` (after a full GitHub refresh: every open pull request in the
    list) lets open cards about other pull requests be checked with `gh`.
    """
    async with _lock:
        cards = await open_cards()
        if not cards:
            return {"concluded": [], "rechecked": []}
        resolved = {c.ref: c for c in changes if c.kind == "resolved"}
        gone = {c.ref: c for c in changes if c.kind == "gone"}
        changed = {c.ref: c for c in changes if c.kind == "changed"}
        card_ref_set = set().union(*(card_refs(r) for rows in cards.values() for r in rows))

        if section == "github":
            verify = {ref: gone[ref].url for ref in gone if ref in card_ref_set}
            if open_refs is not None:
                now = datetime.now(timezone.utc)
                orgs = (config or {}).get("orgs") or []
                stale = sorted(r for r in card_ref_set
                               if r.startswith("gh:") and r not in open_refs and r not in verify
                               and _in_orgs(r, orgs))
                due = (await _due_for_verify(stale, now))[:max(0, GH_VERIFY_LIMIT - len(verify))]
                for ref in due:
                    verify[ref] = None
                    await store.set_state(STATE_PLATFORM, _VERIFY_PREFIX + ref, now.isoformat())
            found = await _verify_github(verify) if verify else {}
            resolved.update(found)
            # A pull request that left the list but is still open left the scope only.
        else:
            # Whether a Linear issue that left the list was completed or only
            # reassigned needs the source: the re-check reads it.
            changed.update({ref: c for ref, c in gone.items() if ref not in resolved})

        concluded = await conclude(resolved, cards) if resolved else []
        closed_entities = {c["entity_id"] for c in concluded}
        remaining = {k: v for k, v in cards.items() if k not in closed_entities}
        rechecked = await _recheck_now(changed, remaining) if changed else []
    if concluded or rechecked:
        log.info("claude_code_related_cards_updated", section=section,
                 concluded=len(concluded), rechecked=rechecked)
    return {"concluded": concluded, "rechecked": rechecked}


async def after_refresh(section: str, old: list[dict[str, Any]] | None, new: list[dict[str, Any]]) -> dict[str, Any]:
    """Update related cards after a section refresh. Never raises."""
    from laya.claude_code.my_work import item_key

    try:
        source = await store.get_source(section)
        if source is None:
            return {}
        changes = diff_items(section, old or [], new) if old is not None else []
        open_refs = None
        if section == "github":
            open_refs = {r for r in (item_ref("github", item_key("github", i)) for i in new) if r}
        return await apply(section, changes, open_refs=open_refs, config=source.get("config") or {})
    except Exception as e:
        log.warning("claude_code_related_failed", section=section, error=str(e))
        return {}


async def after_row_action(section: str, key: str, old: dict[str, Any] | None,
                           new: dict[str, Any] | None, final: dict[str, Any] | None = None) -> dict[str, Any]:
    """Update related cards after a My work row action. Never raises."""
    try:
        change = diff_row(section, key, old, new, final)
        if change is None:
            return {}
        return await apply(section, [change])
    except Exception as e:
        log.warning("claude_code_related_failed", section=section, error=str(e))
        return {}


def schedule(coro) -> None:
    """Run an update in the background; the refresh or action never waits for it."""
    from laya.tasks import create_task

    create_task(coro, name="claude_code_my_work_related")
