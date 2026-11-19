# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Normalization, grouping and filters of the Claude Code collectors."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from laya.claude_code.platforms import common, github, gmail, linear, notion, slack

@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    """No test of the connector may spawn `claude` or `gh`."""
    from laya.claude_code import cli as _cli

    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(_cli, "_run_process", side_effect=_refuse), \
         patch.object(_cli, "read_init", side_effect=_refuse), \
         patch.object(_cli, "cli_version", side_effect=_refuse), \
         patch.object(_cli, "run_gh", side_effect=_refuse):
        yield


ME = "U_ME"
TEAM = {
    "members": [
        {"name": "Kim Self", "email": "kim@company.com", "role": "self",
         "aliases": ["kim@personal.com"], "accounts": [ME, "kim-gh"]},
        {"name": "Mike", "email": "mike@company.com", "role": "teammate"},
    ]
}


def _slack(ts, user="U_MIKE", *, channel="C1", ctype="public", thread=None, text="hi",
           name="Mike", ctx=False, cname="general"):
    return slack.normalize_message({
        "channel_id": channel, "channel_name": cname, "channel_type": ctype, "ts": ts,
        "thread_ts": thread, "user_id": user, "user_name": name, "text": text,
        "permalink": f"https://x.slack.com/{channel}/p{ts}", "is_context": ctx,
    })


# ── common ───────────────────────────────────────────────────────────────


def test_event_id_is_stable_and_platform_scoped():
    assert common.event_id("slack", "C1:1.0") == common.event_id("slack", "C1:1.0")
    assert common.event_id("slack", "C1:1.0") != common.event_id("gmail", "C1:1.0")
    assert common.event_id("slack", "x").startswith("evt_")


def test_parse_ts_formats():
    assert common.parse_ts("1700000000.5").timestamp() == 1700000000.5
    assert common.parse_ts("2026-02-22T10:00:00Z") == datetime(2026, 2, 22, 10, tzinfo=timezone.utc)
    assert common.parse_ts("2026-02-22T10:00:00").tzinfo is not None
    assert common.parse_ts(1700000000).year == 2023


def test_split_conversation():
    mine, after, before = common.split_conversation([1, 2, 3, 4], lambda m: m == 2)
    assert (mine, after, before) == (2, [3, 4], [1, 2])
    assert common.split_conversation([1, 2], lambda m: False) == (None, [1, 2], [])
    assert common.split_conversation([1, 2], lambda m: m == 2) == (2, [], [1, 2])


def test_make_event_conventions():
    e = common.make_event(
        platform="slack", native_id="C1:1.0", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        raw_event_type="message_received", actor_name="A", actor_email="A@X.COM", actor_handle=None,
        subject_type="thread", subject_id="C1:1.0", subject_title="t", subject_url=None,
        body="b", metadata={"k": 1},
    )
    assert e.source.connection_id == "claude_code:slack"
    assert e.content.metadata == {"k": 1, "claude_code": True}
    assert e.actor.email == "a@x.com"
    assert e.entity_id == "slack:thread:C1:1.0"


@pytest.mark.asyncio
async def test_enrich_hook_runs_and_survives_failure():
    calls = []

    async def ok(events, source):
        calls.append("ok")
        return events[:1]

    async def boom(events, source):
        raise RuntimeError("x")

    with patch.object(common, "ENRICHERS", [boom, ok]):
        out = await common.enrich(["a", "b"], {})
    assert out == ["a"] and calls == ["ok"]


# ── slack ────────────────────────────────────────────────────────────────


def test_slack_channel_type_normalization():
    assert _slack("1", ctype="im")["channel_type"] == "dm"
    assert _slack("1", ctype="Direct Message")["channel_type"] == "dm"
    assert _slack("1", channel="D9", ctype="")["channel_type"] == "dm"
    assert _slack("1", ctype="mpim")["channel_type"] == "group_dm"
    assert _slack("1", ctype="private_channel")["channel_type"] == "private"
    assert _slack("1", ctype="channel")["channel_type"] == "public"
    assert _slack("1", ctype="")["channel_type"] == "public"


def test_slack_conversation_subject_ids():
    assert slack.conversation_of(_slack("5.0", channel="D1", ctype="im")) == "D1"
    assert slack.conversation_of(_slack("5.0", channel="G1", ctype="mpim")) == "G1"
    assert slack.conversation_of(_slack("6.0", thread="5.0")) == "C1:5.0"
    assert slack.conversation_of(_slack("5.0")) == "C1:5.0"  # a root is its own thread


def test_slack_relevance_filter():
    threads = {"C1:5.0"}
    assert slack.is_relevant(_slack("1", ctype="im", channel="D1"), ME, threads)
    assert slack.is_relevant(_slack("1", user=ME), ME, threads)
    assert slack.is_relevant(_slack("1", text=f"hey <@{ME}>"), ME, threads)
    assert slack.is_relevant(_slack("6.0", thread="5.0"), ME, threads)
    assert not slack.is_relevant(_slack("7.0", thread="9.0"), ME, threads)


def test_slack_one_event_per_conversation_with_context():
    msgs = [
        _slack("100.0", thread="100.0", ctx=True, text="root question"),
        _slack("101.0", thread="100.0", text="first"),
        _slack("102.0", thread="100.0", text="second", name="Ana", user="U_ANA"),
    ]
    events = slack.build_events(msgs, ME, context_messages=8)
    assert len(events) == 1
    e = events[0]
    assert e.subject.type == "thread" and e.subject.id == "C1:100.0"
    assert e.entity_id == "slack:thread:C1:100.0"
    assert e.actor.platform_handle == "U_ANA"
    assert e.subject.url.endswith("p101.0")  # the first unanswered message
    assert e.source.raw_event_type == "thread_reply"
    body = e.content.body
    assert body.startswith("Earlier in the conversation:\n[")
    assert "Mike: root question" in body
    assert "\n\nNew:\n" in body and body.index("first") < body.index("second")
    md = e.content.metadata
    assert md["slack_channel_id"] == "C1" and md["slack_thread_ts"] == "100.0"
    assert md["slack_channel"] == "C1"  # read by Slack egress
    from laya.egress.platforms.slack import SlackPlatform
    assert SlackPlatform().identifiers_from_event("reply_to_thread", e.event_id, md, {})["channel"] == "C1"
    assert md["slack_channel_type"] == "public" and md["slack_channel_name"] == "general"
    assert md["claude_code"] is True and md["slack_message_count"] == 2


def test_slack_user_wrote_last_gives_only_self_event():
    msgs = [_slack("1.0", channel="D1", ctype="im"), _slack("2.0", channel="D1", ctype="im", user=ME, name="Kim")]
    events = slack.build_events(msgs, ME)
    assert len(events) == 1
    e = events[0]
    assert e.actor.platform_handle == ME
    assert e.subject.id == "D1"
    assert e.subject.title == "DM with Mike"  # titled after the other person
    assert "Me: hi" in e.content.body


def test_slack_user_wrote_in_the_middle():
    msgs = [
        _slack("1.0", channel="D1", ctype="im", text="ask"),
        _slack("2.0", channel="D1", ctype="im", user=ME, text="answer"),
        _slack("3.0", channel="D1", ctype="im", text="follow-up"),
    ]
    events = slack.build_events(msgs, ME, context_messages=8)
    assert [e.actor.platform_handle for e in events] == [ME, "U_MIKE"]
    assert events[0].timestamp < events[1].timestamp
    follow = events[1]
    assert "Earlier in the conversation:" in follow.content.body
    assert "Me: answer" in follow.content.body
    assert follow.content.body.split("New:\n")[1].count("\n") == 0


def test_slack_context_is_capped():
    ctx = [_slack(f"{i}.0", thread="0.0", ctx=True, text=f"c{i}") for i in range(1, 12)]
    events = slack.build_events(ctx + [_slack("50.0", thread="0.0", text="new")], ME, context_messages=3)
    earlier = events[0].content.body.split("\n\nNew:")[0]
    assert earlier.count("\n[") == 3 and "c11" in earlier and "c8" not in earlier


def test_slack_identity_contract_resolves_self():
    """The collector emits the Slack user id that connect writes into team.json."""
    import asyncio

    from laya.pipeline.ingest import resolve_actor_relationship

    e = slack.build_events([_slack("1.0", user=ME, name="Kim")], ME)[0]
    with patch("laya.pipeline.ingest.load_team", return_value=TEAM):
        assert asyncio.run(resolve_actor_relationship(e)) == "self"


@pytest.mark.asyncio
async def test_slack_collect_filters_and_remembers_threads(db):
    out = {"ws": "acme", "ch": [{"id": "C1", "nm": "general", "ty": "channel"},
                                 {"id": "C2", "nm": "random", "ty": "channel"}],
           "m": [
        {"q": "o", "c": "C1", "ts": "10.0", "th": "9.0", "u": ME, "n": "Kim", "t": "mine"},
        {"q": "x", "c": "C2", "ts": "11.0", "th": None, "u": "U_X", "n": "X", "t": "chatter"},
        {"q": "w", "c": "C1", "ts": "12.0", "th": "9.0", "u": "U_X", "n": "X", "t": "reply in my thread"},
    ]}
    source = {"config": slack_config(), "identity": {"slack_user_id": ME},
              "tools": {"search": "s", "read_thread": "t", "read_channel": "c"}}
    with patch.object(slack, "run_turn", new_callable=AsyncMock, return_value=out) as turn:
        events = await slack.collect(source, datetime(2026, 1, 1, tzinfo=timezone.utc))
    # The user answered inside the sweep: no context turn.
    assert turn.await_count == 1
    prompt = turn.await_args.args[2]
    assert f"with:<@{ME}>" in prompt and f"keywords=[\"<@{ME}>\"]" in prompt and "include_context=false" in prompt
    assert {e.subject.id for e in events} == {"C1:9.0"}
    reply = next(e for e in events if e.actor.platform_handle == "U_X")
    assert reply.subject.url == "https://acme.slack.com/archives/C1/p120?thread_ts=9.0&cid=C1"
    from laya.claude_code import store
    assert "thread:C1:9.0" in await store.get_state("slack", "thread:")
    assert "seen:C1:9.0" in await store.get_state("slack", "seen:")


@pytest.mark.asyncio
async def test_slack_context_read_only_for_unseen_conversations_with_others(db):
    from laya.claude_code import store

    await store.set_state("slack", "seen:D2", datetime.now(timezone.utc).isoformat())
    search = {"ws": "acme", "ch": [
        {"id": "D1", "ty": "dm", "pp": ["Mike", "Kim"]},
        {"id": "D2", "ty": "dm", "pp": ["Ana", "Kim"]},
        {"id": "C1", "nm": "eng", "ty": "channel"},
        {"id": "C3", "nm": "ops", "ty": "channel"},
    ], "m": [
        {"q": "w", "c": "D1", "ts": "100.0", "u": "U_MIKE", "n": "Mike", "t": "new in D1"},
        {"q": "w", "c": "D2", "ts": "101.0", "u": "U_ANA", "n": "Ana", "t": "already seen DM"},
        {"q": "m", "c": "C1", "ts": "102.0", "th": "50.0", "u": "U_ANA", "n": "Ana", "t": f"<@{ME}> look"},
        {"q": "m", "c": "C3", "ts": "103.0", "u": "U_ANA", "n": "Ana", "t": f"root <@{ME}>"},
        {"q": "o", "c": "D1", "ts": "99.0", "u": ME, "n": "Kim", "t": "own earlier"},
    ]}
    context = {"m": [
        {"c": "C1", "ts": "50.0", "th": "50.0", "u": "U_MIKE", "n": "Mike", "t": "thread root"},
        {"c": "C9", "ts": "1.0", "u": "U_MIKE", "n": "Mike", "t": "not asked for"},
    ]}
    source = {"config": slack_config(), "identity": {"slack_user_id": ME, "name": "Kim"},
              "tools": {"search": "s", "read_thread": "rt", "read_channel": "rc"}}
    with patch.object(slack, "run_turn", new_callable=AsyncMock, side_effect=[search, context]) as turn:
        events = await slack.collect(source, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert turn.await_count == 2
    ctx_prompt = turn.await_args_list[1].args[2]
    # D1 has the user's own message in this sweep, D2 was delivered before, C3 is a root.
    assert "channel_id=C1 message_ts=50.0 latest=102.0" in ctx_prompt
    assert "D1" not in ctx_prompt and "D2" not in ctx_prompt and "C3" not in ctx_prompt
    assert turn.await_args_list[1].args[3] is slack.CONTEXT_SCHEMA
    thread = next(e for e in events if e.subject.id == "C1:50.0")
    assert "Mike: thread root" in thread.content.body
    assert all("not asked for" not in e.content.body for e in events)


def test_slack_context_wanted_caps_and_orders():
    msgs = [_slack(f"{i}.0", channel=f"D{i}", ctype="im") for i in range(1, 15)]
    wanted = slack.context_wanted(msgs, ME, set(), limit=3)
    assert [w["channel_id"] for w in wanted] == ["D14", "D13", "D12"]
    assert all(w["thread_ts"] is None for w in wanted)


def test_slack_expand_message_builds_permalink_and_caps_text():
    channels = {"C1": {"id": "C1", "nm": "eng", "ty": "channel"}}
    m = slack.expand_message({"c": "C1", "ts": "p1700000000123456", "u": "U1", "n": "A", "t": "x" * 5000},
                             channels, "acme", context=False)
    assert m["ts"] == "1700000000.123456"
    assert m["permalink"] == "https://acme.slack.com/archives/C1/p1700000000123456"
    assert len(m["text"]) == slack.TEXT_CHARS and m["channel_name"] == "eng"
    assert slack.expand_message({"c": "C1", "ts": "1.0", "u": "U", "t": ""}, {}, None, context=True)["permalink"] is None


def test_slack_group_dm_with_d_id_is_not_a_dm():
    msgs = [
        _slack("1.0", channel="D7", ctype="group_dm", name="Euan", user="U_E"),
        _slack("2.0", channel="D7", ctype="dm", name="Roland", user="U_R"),
    ]
    for m in msgs:
        m.update(participants=["Euan", "Kim Self", "Roland"])
    msgs = [slack.normalize_message(m) for m in msgs]
    assert {m["channel_type"] for m in msgs} == {"group_dm"}
    events = slack.build_events(msgs, ME, my_names={"Kim Self"})
    assert events[0].subject.title == "Group DM: Euan, Roland"
    assert events[0].content.metadata["slack_channel_type"] == "group_dm"
    # A 1:1 DM with a D id stays a DM.
    assert _slack("1", channel="D8", ctype="")["channel_type"] == "dm"


def test_slack_group_dm_title_without_participants_uses_senders():
    msgs = [_slack("1.0", channel="G1", ctype="mpim", name="Euan", user="U_E"),
            _slack("2.0", channel="G1", ctype="mpim", name="Kim", user=ME)]
    events = slack.build_events(msgs, ME)
    assert {e.subject.title for e in events} == {"Group DM: Euan"}


def slack_config(**kw):
    from laya.claude_code.store import default_config
    return {**default_config("slack"), **kw}


@pytest.mark.asyncio
async def test_slack_collect_requires_identity(db):
    with pytest.raises(RuntimeError):
        await slack.collect({"config": slack_config(), "identity": {}, "tools": {}}, datetime.now(timezone.utc))


# ── gmail ────────────────────────────────────────────────────────────────


def _mail(mid, thread="t1", sender="mike@company.com", date="2026-02-22T10:00:00Z", kind="email", body="hello"):
    return {"message_id": mid, "thread_id": thread, "date": date, "from_name": sender.split("@")[0],
            "from_email": sender, "subject": "Budget", "body": body, "url": f"https://mail/{mid}",
            "kind": kind, "to": ["kim@company.com"]}


MINE = {"kim@company.com", "kim@personal.com"}


def test_gmail_thread_grouping_and_self_split():
    msgs = [
        _mail("m1", date="2026-02-22T09:00:00Z"),
        _mail("m2", sender="KIM@company.com", date="2026-02-22T10:00:00Z"),
        _mail("m3", date="2026-02-22T11:00:00Z", body="one"),
        _mail("m4", date="2026-02-22T12:00:00Z", body="two"),
        _mail("x1", thread="t2"),
    ]
    events = gmail.build_events(msgs, MINE)
    by_thread = {}
    for e in events:
        by_thread.setdefault(e.subject.id, []).append(e)
    assert [e.actor.email for e in by_thread["t1"]] == ["kim@company.com", "mike@company.com"]
    later = by_thread["t1"][1]
    assert later.subject.type == "email_thread"
    assert later.entity_id == "gmail:email_thread:t1"
    assert "one" in later.content.body and "two" in later.content.body
    assert later.subject.url == "https://mail/m3"
    assert later.content.metadata["gmail_thread_id"] == "t1"
    assert later.content.metadata["gmail_message_count"] == 2
    assert len(by_thread["t2"]) == 1


def test_gmail_meeting_notes_one_event_per_action_item():
    body = (
        "Weekly sync 22.02\n"
        "Action items for Kim:\n- Send the budget: by Friday\n- Review the deck\n"
        "Decisions:\n- Ship v2\n"
        "Waiting on others:\n- Mike sends numbers\n"
    )
    m = _mail("n1", thread="tn", sender="gemini-notes@google.com", kind="meeting_notes", body=body)
    events = gmail.build_events([m], MINE)
    assert len(events) == 2
    first = events[0]
    assert first.subject.type == "meeting" and first.subject.id == "tn:item:0"
    assert first.subject.title == "Send the budget (Weekly sync 22.02)"
    assert first.actor.email == "gemini-notes@google.com"
    assert "Decisions in that meeting:\n- Ship v2" in first.content.body
    assert "Waiting on others in that meeting:" in first.content.body
    assert first.content.metadata["meeting"] == "Weekly sync 22.02"
    assert events[1].subject.id == "tn:item:1"
    assert events[0].event_id != events[1].event_id


def test_gmail_meeting_without_items_yields_nothing():
    m = _mail("n1", sender="gemini-notes@google.com", kind="meeting_notes", body="Sync\nDecisions:\n- x")
    assert gmail.build_events([m], MINE) == []


def test_gmail_prompt_excludes_senders_and_meeting_step():
    tools = {"search": "S", "get_thread": "G", "drive_read": "D"}
    p = gmail.build_prompt(tools, datetime(2026, 1, 1, tzinfo=timezone.utc), ["a@b.c"], "Kim", True)
    assert "-from:a@b.c" in p and "D" in p and "Action items for Kim:" in p
    p = gmail.build_prompt(tools, datetime(2026, 1, 1, tzinfo=timezone.utc), [], "Kim", False)
    assert "meeting_notes" not in p


# ── linear ───────────────────────────────────────────────────────────────


def _lin(nid, ident="TRAD-1", kind="comment", status="Todo", email="mike@company.com",
         at="2026-02-22T10:00:00Z", text="please look"):
    return {"id": nid, "created_at": at, "kind": kind, "issue_identifier": ident,
            "issue_title": "Fix it", "issue_url": f"https://linear.app/x/{ident}",
            "issue_status": status, "actor_name": email.split("@")[0], "actor_email": email, "text": text}


def test_linear_done_becomes_closing_event():
    events = linear.build_events([_lin("n1"), _lin("n2", status="Done", at="2026-02-22T11:00:00Z")], MINE)
    assert len(events) == 1
    e = events[0]
    assert e.content.metadata["resolved_in_source"] is True
    assert e.subject.type == "ticket" and e.subject.id == "TRAD-1"
    assert e.entity_id == "linear:ticket:TRAD-1"
    # A distinct id from the plain notification, which may have been delivered already.
    assert e.event_id != linear.build_events([_lin("n2")], MINE)[0].event_id


def test_linear_review_requests_are_marked():
    events = linear.build_events([
        _lin("r1", kind="review_requested"),
        _lin("r2", ident="TRAD-2", text="Mike requested your review on PR #4"),
    ], MINE)
    assert [e.source.raw_event_type for e in events] == ["review_requested", "review_requested"]


def test_linear_self_split_and_grouping():
    events = linear.build_events([
        _lin("a", at="2026-02-22T09:00:00Z"),
        _lin("b", email="KIM@company.com", at="2026-02-22T10:00:00Z"),
        _lin("c", at="2026-02-22T11:00:00Z", text="x"),
        _lin("d", at="2026-02-22T12:00:00Z", text="y"),
        _lin("z", ident=""),
    ], MINE)
    assert [e.actor.email for e in events] == ["kim@company.com", "mike@company.com"]
    assert "x" in events[1].content.body and "y" in events[1].content.body
    assert events[1].content.metadata["linear_status"] == "Todo"


# ── notion ───────────────────────────────────────────────────────────────


def test_notion_conversations():
    items = [
        {"id": "c1", "created_at": "2026-02-22T10:00:00Z", "kind": "comment", "page_id": "pg",
         "page_title": "Plan", "page_url": "https://notion.so/pg", "discussion_id": "d1",
         "author_name": "Mike", "author_email": "mike@company.com", "text": "q?"},
        {"id": "c2", "created_at": "2026-02-22T11:00:00Z", "kind": "comment", "page_id": "pg",
         "page_title": "Plan", "page_url": "https://notion.so/pg", "discussion_id": "d1",
         "author_name": "Kim", "author_email": "kim@personal.com", "text": "a"},
        {"id": "pg:t", "created_at": "2026-02-22T12:00:00Z", "kind": "mention", "page_id": "pg",
         "page_title": "Plan", "page_url": "https://notion.so/pg", "discussion_id": None,
         "author_name": "Mike", "author_email": "mike@company.com", "text": "@Kim see"},
    ]
    events = notion.build_events(items, MINE)
    assert {(e.subject.id, e.actor.email) for e in events} == {
        ("pg:d1", "kim@personal.com"), ("pg", "mike@company.com"),
    }
    mention = next(e for e in events if e.subject.id == "pg")
    assert mention.source.raw_event_type == "page_mention"
    assert mention.content.metadata["notion_page_id"] == "pg"


# ── github ───────────────────────────────────────────────────────────────


def _notif(nid, reason="comment", typ="PullRequest", number=7, owner="org", comment=True):
    url = f"https://api.github.com/repos/{owner}/repo/{'pulls' if typ == 'PullRequest' else 'issues'}/{number}"
    return {
        "id": nid, "reason": reason, "updated_at": "2026-02-22T10:00:00Z",
        "repository": {"full_name": f"{owner}/repo", "owner": {"login": owner}},
        "subject": {"type": typ, "title": "Fix", "url": url,
                    "latest_comment_url": f"{url}/comments/1" if comment else None},
    }


def _gh(responses):
    async def fake(*args, **kwargs):
        key = " ".join(args)
        for prefix, value in responses.items():
            if prefix in key:
                return value
        raise AssertionError(f"unexpected gh call: {key}")
    return fake


@pytest.mark.asyncio
async def test_github_collect():
    pr_url = "https://api.github.com/repos/org/repo/pulls"
    responses = {
        "notifications?": [[
            _notif("1", reason="comment", number=7),        # own PR, human comment
            _notif("2", reason="comment", number=8),        # someone else's PR -> skipped
            _notif("3", reason="mention", number=9),        # mention on another PR -> kept
            _notif("4", reason="comment", typ="Issue", number=10),
            _notif("5", reason="ci_activity", typ="CheckSuite"),
            _notif("6", reason="comment", owner="other", number=11),  # org filter
        ]],
        f"{pr_url}/7/comments/1": {"body": "please fix", "user": {"login": "mike"}, "html_url": "https://github.com/org/repo/pull/7#c1"},
        f"{pr_url}/9/comments/1": {"body": "@kim-gh look", "user": {"login": "ana"}, "html_url": "h9"},
        "issues/10/comments/1": {"body": "bot says", "user": {"login": "ci[bot]"}, "html_url": "h10"},
        f"{pr_url}/7": {"user": {"login": "kim-gh"}},
        f"{pr_url}/8": {"user": {"login": "someone"}},
        "search/issues": {"items": [
            {"repository_url": "https://api.github.com/repos/org/repo", "number": 7, "title": "Fix",
             "html_url": "https://github.com/org/repo/pull/7", "closed_at": "2026-02-22T12:00:00Z",
             "pull_request": {"merged_at": "2026-02-22T12:00:00Z"}},
            {"repository_url": "https://api.github.com/repos/org/repo", "number": 12, "title": "Old",
             "html_url": "https://github.com/org/repo/pull/12", "closed_at": "2026-02-22T12:00:00Z",
             "pull_request": {"merged_at": None}},
        ]},
    }
    source = {"config": {"orgs": ["org"], "others_prs_only_on_mention": True},
              "identity": {"github_login": "kim-gh"}}
    with patch.object(github, "run_gh", side_effect=_gh(responses)):
        events = await github.collect(source, datetime(2026, 2, 22, tzinfo=timezone.utc))

    by_id = {(e.subject.id, e.source.raw_event_type): e for e in events}
    assert set(by_id) == {
        ("org/repo#7", "comment"), ("org/repo#9", "mention"), ("org/repo#10", "comment"),
        ("org/repo#7", "pr_merged"), ("org/repo#12", "pr_closed"),
    }
    own = by_id[("org/repo#7", "comment")]
    assert own.subject.type == "pull_request" and own.actor.platform_handle == "mike"
    assert own.entity_id == "github:pull_request:org/repo#7"
    assert own.content.body == "please fix"
    assert by_id[("org/repo#10", "comment")].subject.type == "ticket"
    assert by_id[("org/repo#10", "comment")].actor.platform_handle == "ci[bot]"
    merged = by_id[("org/repo#7", "pr_merged")]
    assert merged.content.metadata["resolved_in_source"] is True
    assert merged.actor.platform_handle == "kim-gh"
    assert merged.entity_id == own.entity_id


def test_github_html_url():
    assert github.html_url("https://api.github.com/repos/o/r/pulls/5") == "https://github.com/o/r/pull/5"
    assert github.html_url(None) is None


def test_slack_ts_without_dot_is_normalized():
    assert slack.normalize_ts("1795011351868629") == "1795011351.868629"
    assert slack.normalize_ts("p1795011351868629") == "1795011351.868629"
    assert slack.normalize_ts("1795011351.868629") == "1795011351.868629"
    assert slack.normalize_ts(None) is None
    m = _slack("1795011351868629", thread="1795011300000100")
    assert m["ts"] == "1795011351.868629" and m["thread_ts"] == "1795011300.000100"
    assert slack.conversation_of(m) == "C1:1795011300.000100"


@pytest.mark.asyncio
async def test_gmail_collect_unescapes_html_and_builds_links(db):
    out = {"m": [{"id": "m1", "th": "t1", "d": "2026-02-22T10:00:00Z", "fn": "Mike",
                  "fe": "mike@company.com", "s": "A &amp; B", "b": "say &quot;hi&quot;" + "x" * 5000,
                  "k": "email", "to": ["kim@company.com"]}]}
    source = {"config": {}, "tools": {"search": "S", "get_thread": "G", "get_message": "M"},
              "identity": {"email": "kim@company.com"}}
    with patch.object(gmail, "run_turn", new_callable=AsyncMock, return_value=out) as turn, \
         patch("laya.config.get_self_user", return_value=None):
        events = await gmail.collect(source, datetime(2026, 1, 1, tzinfo=timezone.utc))
    e = events[0]
    assert e.content.body.startswith('say "hi"') and len(e.content.body) == gmail.BODY_CHARS
    assert e.subject.title == "A & B"
    assert e.subject.url == "https://mail.google.com/mail/?authuser=kim%40company.com#all/t1"
    prompt = turn.await_args.args[2]
    # Bodies are read per message from others, own mails keep their snippet.
    assert "call M (messageFormat PLAIN_TEXT)" in prompt and "THREAD_VIEW_MINIMAL" in prompt
    assert "snippet" in prompt and str(gmail.BODY_CHARS) in prompt


def test_gmail_prompt_falls_back_to_get_thread():
    p = gmail.build_prompt({"search": "S", "get_thread": "G"}, datetime(2026, 1, 1, tzinfo=timezone.utc),
                           [], "Kim", False)
    assert "call G (messageFormat PLAIN_TEXT) once for its thread" in p


def test_gmail_meeting_notes_keep_document_link():
    m = gmail.expand_message({"id": "m", "th": "t", "d": "2026-01-01T00:00:00Z", "fe": gmail.MEETING_NOTES_SENDER,
                              "s": "Notes", "b": "Sync\nAction items for Kim:\n- do it", "k": "meeting_notes",
                              "u": "https://docs.google.com/document/d/x"})
    assert m["url"] == "https://docs.google.com/document/d/x" and m["kind"] == "meeting_notes"


def test_gmail_event_carries_newest_message_id():
    msgs = [_mail("m1", date="2026-02-22T10:00:00Z"), _mail("m2", date="2026-02-22T11:00:00Z")]
    e = gmail.build_events(msgs, MINE)[0]
    assert e.content.metadata["gmail_message_id"] == "m2"
    own = gmail.build_events([_mail("m3", sender="kim@company.com")], MINE)[0]
    assert own.content.metadata["gmail_message_id"] == "m3"


@pytest.mark.asyncio
async def test_notion_collect_expands_compact_items(db):
    out = {"items": [{"id": "c1", "at": "2026-02-22T10:00:00Z", "k": "comment", "p": "0123-abcd",
                      "pt": "Plan", "d": "d1", "an": "Mike", "ae": "mike@company.com", "t": "x" * 3000}]}
    source = {"config": {}, "tools": {"search": "S", "comments": "C", "fetch": "F"}, "identity": {}}
    with patch.object(notion, "run_turn", new_callable=AsyncMock, return_value=out) as turn, \
         patch("laya.config.get_self_user", return_value=None):
        [e] = await notion.collect(source, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert e.subject.id == "0123-abcd:d1" and e.subject.url == "https://www.notion.so/0123abcd"
    assert len(e.content.body) == notion.TEXT_CHARS
    prompt = turn.await_args.args[2]
    assert "max_highlight_length 0" in prompt and "Do not fetch pages" in prompt


@pytest.mark.asyncio
async def test_linear_collect_expands_compact_items(db):
    out = {"ws": "acme", "items": [{"id": "n1", "at": "2026-02-22T10:00:00Z", "k": "comment", "i": "TRAD-9",
                                    "it": "Fix", "st": "Todo", "an": "Mike", "ae": "mike@company.com",
                                    "t": "please"}]}
    source = {"config": {}, "tools": {"notifications": "N", "list_issues": "L", "get_issue": "G"}, "identity": {}}
    with patch.object(linear, "run_turn", new_callable=AsyncMock, return_value=out) as turn, \
         patch("laya.config.get_self_user", return_value=None):
        [e] = await linear.collect(source, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert e.subject.url == "https://linear.app/acme/issue/TRAD-9"
    assert e.subject.title == "TRAD-9: Fix" and e.content.metadata["linear_status"] == "Todo"
    assert f"limit={linear.ISSUES_LIMIT}" in turn.await_args.args[2]


def test_slack_dm_title_from_participants_when_only_the_user_wrote():
    m = _slack("1.0", channel="D1", ctype="dm", user=ME, name="Kim Self")
    m["participants"] = ["Euan Hay", "Kim Self"]
    assert slack.build_events([m], ME, my_names={"Kim Self"})[0].subject.title == "DM with Euan Hay"


def test_slack_mention_with_display_name_counts():
    m = _slack("1.0", text=f"<@{ME}|Kim> can you look")
    assert slack.mentions(m, ME) and slack.is_relevant(m, ME, set())
    assert slack.build_events([m], ME)[0].content.metadata["slack_is_mention"] is True
    assert not slack.mentions(_slack("1.0", text="<@U_MEX> hi"), ME)
