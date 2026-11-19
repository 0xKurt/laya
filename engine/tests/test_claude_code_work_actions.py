# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Direct actions in "My work" (claude_code/work_actions.py): Linear writes
through one-tool `claude -p` turns, GitHub writes through `gh`."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from laya.claude_code import cli, discover, my_work, store, work_actions
from laya.claude_code.discover import Discovery, PlatformDiscovery
from laya.claude_code.work_actions import ActionError

LINEAR = "mcp__linear-server__"
ALL_TOOLS = [
    f"{LINEAR}list_issues", f"{LINEAR}get_issue", f"{LINEAR}save_issue", f"{LINEAR}save_comment",
    f"{LINEAR}list_issue_statuses", f"{LINEAR}delete_comment", "mcp__claude_ai_Slack__slack_send_message",
]
TOOLS = {
    "list_issues": f"{LINEAR}list_issues", "get_issue": f"{LINEAR}get_issue",
    "save_issue": f"{LINEAR}save_issue", "save_comment": f"{LINEAR}save_comment",
    "list_issue_statuses": f"{LINEAR}list_issue_statuses",
}
IDENTITY = {"linear_user_id": "u-kurt", "email": "kurt@acme.dev", "name": "Kurt"}
SHA = "a" * 40
STATES = {"states": [
    {"id": "s1", "n": "In Progress", "t": "started"}, {"id": "s2", "n": "Todo", "t": "unstarted"},
    {"id": "s3", "n": "Done", "t": "completed"}, {"id": "s4", "n": "Backlog", "t": "backlog"},
]}


@pytest.fixture(autouse=True)
def _no_real_subprocesses():
    def _refuse(*a, **k):
        raise AssertionError("a test tried to spawn claude or gh")

    with patch.object(cli, "_run_process", side_effect=_refuse), \
         patch.object(cli, "read_init", side_effect=_refuse), \
         patch.object(cli, "cli_version", side_effect=_refuse), \
         patch.object(cli, "run_gh", side_effect=_refuse), \
         patch.object(cli, "run_gh_write", side_effect=_refuse):
        discover._cache = Discovery(
            all_tools=list(ALL_TOOLS),
            platforms={"linear": PlatformDiscovery(available=True, tools=dict(TOOLS))},
        )
        work_actions._busy.clear()
        yield
        discover.reset_cache()


def _turn(out):
    return {"structured_output": out, "result": "", "is_error": False}


def _raw(ident="TRAD-12", **kw):
    base = {"i": ident, "t": "Fix it", "u": f"https://linear.app/acme/issue/{ident}", "s": "Todo",
            "st": "unstarted", "p": 3, "pr": "Proj", "d": None, "up": "2026-11-19T10:00:00Z",
            "l": [], "a": "Kurt", "aid": "u-kurt"}
    base.update(kw)
    return base


class FakeClaude:
    """Answers the states, write and verify turns; records every call."""

    def __init__(self, write=None, verify=None, states=None):
        self.calls = []
        self.write = write if write is not None else {"ok": True, "id": "x", "url": "", "error": ""}
        self.verify = verify if verify is not None else {"found": True, "issue": _raw()}
        self.states = states if states is not None else STATES

    async def __call__(self, step, prompt, tool, **kw):
        self.calls.append({"step": step, "prompt": prompt, "tool": tool, **kw})
        if step == "linear_states":
            return _turn(self.states)
        if step == "linear_action":
            return _turn(self.write)
        return _turn(self.verify)


async def _connect_linear(**kw):
    await store.upsert_source("linear", tools=dict(TOOLS), identity=dict(IDENTITY), **kw)


def _args_of(call):
    m = call["prompt"].split("ARGUMENTS (JSON):\n", 1)[1]
    return json.loads(m.split("\nDo not call", 1)[0])


# ── Linear: pure parts ───────────────────────────────────────────────────


def test_linear_payloads_always_name_the_issue():
    assert work_actions.linear_payload("state", "TRAD-1", "Done") == ("save_issue", {"id": "TRAD-1", "state": "Done"})
    assert work_actions.linear_payload("assign_me", "TRAD-1", None) == ("save_issue", {"id": "TRAD-1", "assignee": "me"})
    assert work_actions.linear_payload("priority", "TRAD-1", 0) == ("save_issue", {"id": "TRAD-1", "priority": 0})
    role, args = work_actions.linear_payload("comment", "TRAD-1", "hi\nthere")
    # save_comment's `id` would edit an existing comment: only issueId is passed.
    assert role == "save_comment" and args == {"issueId": "TRAD-1", "body": "hi\nthere"}


@pytest.mark.parametrize("action,value", [
    ("priority", 5), ("priority", -1), ("priority", "2"), ("priority", True),
    ("comment", "  "), ("comment", "x" * 20_001), ("state", ""), ("delete", None),
])
def test_linear_payload_rejects_bad_values(action, value):
    with pytest.raises(ActionError):
        work_actions.linear_payload(action, "TRAD-1", value)


def test_identifier_is_validated():
    assert work_actions.check_identifier(" trad-12 ") == "TRAD-12"
    for bad in ("TRAD", "12", "TRAD-12; rm", "P-ENG-12", ""):
        with pytest.raises(ActionError):
            work_actions.check_identifier(bad)


def test_write_prompt_carries_payload_verbatim():
    payload = {"issueId": "TRAD-1", "body": 'Ünïcode "quotes"\n- list\n`code`'}
    p = work_actions.write_prompt("mcp__x__save_comment", payload)
    assert json.loads(p.split("ARGUMENTS (JSON):\n", 1)[1].split("\nDo not call", 1)[0]) == payload
    assert "exactly once" in p and "verbatim" in p


def test_verify_change():
    assert work_actions.verify_change("state", {"state": "Done"}, {"s": "done"}, IDENTITY) is None
    assert "In Progress" in work_actions.verify_change("state", {"state": "Done"}, {"s": "In Progress"}, IDENTITY)
    assert work_actions.verify_change("priority", {"priority": 1}, {"p": 1}, IDENTITY) is None
    assert work_actions.verify_change("priority", {"priority": 1}, {"p": 2}, IDENTITY)
    assert work_actions.verify_change("assign_me", {}, {"aid": "u-kurt"}, IDENTITY) is None
    assert work_actions.verify_change("assign_me", {}, {"a": "kurt@acme.dev"}, IDENTITY) is None
    assert work_actions.verify_change("assign_me", {}, {"a": "Byron", "aid": "u-b"}, IDENTITY)


# ── Linear: turns ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_priority_one_tool_turn_then_verify(db):
    await _connect_linear()
    fake = FakeClaude(verify={"found": True, "issue": _raw(p=1)})
    with patch.object(cli, "run_send", new=fake):
        out = await work_actions.linear_action("TRAD-12", "priority", 1)

    write, verify = fake.calls
    assert write["step"] == "linear_action" and write["tool"] == TOOLS["save_issue"]
    assert set(write["deny"]) == set(ALL_TOOLS) - {TOOLS["save_issue"]}
    assert _args_of(write) == {"id": "TRAD-12", "priority": 1}
    assert verify["tool"] == TOOLS["get_issue"]
    assert set(verify["deny"]) == set(ALL_TOOLS) - {TOOLS["get_issue"]}
    assert out["ok"] and out["issue"]["priority"] == 1 and not out["removed"]
    assert out["issue"]["assigned_to_me"]


@pytest.mark.asyncio
async def test_one_tool_allowlist_reaches_the_cli(db):
    """The real argument list: one allowed tool, every other discovered tool denied."""
    await _connect_linear()
    seen = []

    async def proc(args, timeout):
        seen.append(args)
        step_out = ({"ok": True, "id": "c1", "url": "", "error": ""} if len(seen) == 1
                    else {"found": True, "issue": _raw()})
        return [json.dumps({"structured_output": step_out, "usage": {}})]

    with patch.object(cli, "_run_process", side_effect=proc), \
         patch("laya.llm.client.log_to_audit", new_callable=AsyncMock):
        await work_actions.linear_action("TRAD-12", "comment", "Looks good")

    args = seen[0]
    assert args[args.index("--allowedTools") + 1] == TOOLS["save_comment"]
    denied = args[args.index("--disallowedTools") + 1].split(",")
    assert TOOLS["save_comment"] not in denied and TOOLS["save_issue"] in denied
    assert args[args.index("--tools") + 1] == ""
    assert seen[1][seen[1].index("--allowedTools") + 1] == TOOLS["get_issue"]


@pytest.mark.asyncio
async def test_state_change_uses_team_states_and_drops_completed_rows(db):
    await _connect_linear()
    await store.set_state(my_work.STATE_PLATFORM, "linear", json.dumps({
        "items": [{"identifier": "TRAD-12", "created_by_me": True}, {"identifier": "TRAD-13"}],
        "fetched_at": "2026-11-19T00:00:00+00:00"}))
    fake = FakeClaude(verify={"found": True, "issue": _raw(s="Done", st="completed")})
    with patch.object(cli, "run_send", new=fake):
        out = await work_actions.linear_action("TRAD-12", "state", "done")
        # The second action reads the states from the cache.
        fake.verify = {"found": True, "issue": _raw("TRAD-13", s="In Progress", st="started")}
        await work_actions.linear_action("TRAD-13", "state", "In Progress")

    steps = [c["step"] for c in fake.calls]
    assert steps == ["linear_states", "linear_action", "linear_verify", "linear_action", "linear_verify"]
    assert fake.calls[0]["tool"] == TOOLS["list_issue_statuses"]
    assert _args_of(fake.calls[1]) == {"id": "TRAD-12", "state": "Done"}  # the team's spelling
    assert out["removed"]
    cache = json.loads((await store.get_state(my_work.STATE_PLATFORM, "linear"))["linear"])
    assert [r["identifier"] for r in cache["items"]] == ["TRAD-13"]
    assert cache["items"][0]["state"] == "In Progress"


@pytest.mark.asyncio
async def test_unknown_state_is_refused_before_any_write(db):
    await _connect_linear()
    fake = FakeClaude()
    with patch.object(cli, "run_send", new=fake), pytest.raises(ActionError, match="not a workflow state"):
        await work_actions.linear_action("TRAD-12", "state", "Shipped")
    assert [c["step"] for c in fake.calls] == ["linear_states"]


@pytest.mark.asyncio
async def test_team_states_sorted_and_cached(db):
    await _connect_linear()
    fake = FakeClaude()
    with patch.object(cli, "run_send", new=fake):
        states = await work_actions.team_states("trad")
        await work_actions.team_states("TRAD")
        assert len(fake.calls) == 1
        await work_actions.team_states("TRAD", refresh=True)
        assert len(fake.calls) == 2
    assert [s["type"] for s in states] == ["backlog", "unstarted", "started", "completed"]
    assert '"team": "TRAD"' in fake.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_linear_errors_are_reported_and_not_retried(db):
    await _connect_linear()
    fake = FakeClaude(write={"ok": False, "id": "", "url": "", "error": "Issue not found"})
    with patch.object(cli, "run_send", new=fake), pytest.raises(ActionError, match="Issue not found") as e:
        await work_actions.linear_action("TRAD-12", "assign_me")
    assert e.value.status == 502 and len(fake.calls) == 1

    fake = FakeClaude(verify={"found": True, "issue": _raw(a="Byron", aid="u-b")})
    with patch.object(cli, "run_send", new=fake), pytest.raises(ActionError, match="did not take effect"):
        await work_actions.linear_action("TRAD-12", "assign_me")

    failing = AsyncMock(side_effect=cli.ClaudeCodeError("timed out"))
    with patch.object(cli, "run_send", failing), pytest.raises(ActionError, match="may have been applied"):
        await work_actions.linear_action("TRAD-12", "priority", 2)
    assert failing.await_count == 1


@pytest.mark.asyncio
async def test_permission_denial_fails_the_action(db):
    await _connect_linear()

    async def denied(step, prompt, tool, **kw):
        return {"structured_output": {"ok": True, "id": "x", "url": "", "error": ""},
                "permission_denials": [{"tool_name": f"{LINEAR}delete_comment"}]}

    with patch.object(cli, "run_send", side_effect=denied), pytest.raises(ActionError, match="delete_comment"):
        await work_actions.linear_action("TRAD-12", "comment", "hi")


@pytest.mark.asyncio
async def test_linear_not_connected_or_tool_missing(db):
    with pytest.raises(ActionError, match="not connected") as e:
        await work_actions.linear_action("TRAD-12", "assign_me")
    assert e.value.status == 404
    await store.upsert_source("linear", tools={"get_issue": TOOLS["get_issue"]})
    discover._cache.platforms["linear"].tools = {"get_issue": TOOLS["get_issue"]}
    with pytest.raises(ActionError, match="save_comment"):
        await work_actions.linear_action("TRAD-12", "comment", "hi")


@pytest.mark.asyncio
async def test_linear_dry_run_builds_commands_without_running(db):
    await _connect_linear()
    send = AsyncMock()
    with patch.object(cli, "run_send", send), patch("laya.config.get_agent_binary", return_value="claude"):
        out = await work_actions.linear_action("TRAD-12", "assign_me", dry_run=True)
    send.assert_not_awaited()
    cmd = out["command"]
    assert out["dry_run"] and cmd[cmd.index("--allowedTools") + 1] == TOOLS["save_issue"]
    assert out["payload"] == {"id": "TRAD-12", "assignee": "me"}


@pytest.mark.asyncio
async def test_concurrent_click_on_same_row_is_refused(db):
    await _connect_linear()
    work_actions._busy.add("TRAD-12")
    with patch.object(cli, "run_send", new=FakeClaude()), pytest.raises(ActionError) as e:
        await work_actions.linear_action("TRAD-12", "assign_me")
    assert e.value.status == 409


# ── GitHub ───────────────────────────────────────────────────────────────


def _pr_node(**kw):
    node = {
        "state": "OPEN", "number": 7, "title": "Fix", "url": "https://github.com/acme/app/pull/7",
        "isDraft": False, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-02T00:00:00Z",
        "baseRefName": "main", "headRefName": "kurt/fix", "headRefOid": SHA, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED", "author": {"login": "kurt"},
        "repository": {"nameWithOwner": "acme/app", "mergeCommitAllowed": False,
                       "squashMergeAllowed": True, "rebaseMergeAllowed": True, "deleteBranchOnMerge": True},
        "reviewRequests": {"totalCount": 0}, "latestReviews": {"nodes": []},
        "reviewThreads": {"nodes": []}, "commits": {"nodes": []},
    }
    node.update(kw)
    return node


def _gh_read(viewer="kurt", **kw):
    return {"data": {"viewer": {"login": viewer}, "repository": {"pullRequest": _pr_node(**kw)}}}


def test_parse_pr_has_merge_settings():
    pr = my_work.parse_pr(_pr_node())
    assert pr["merge_methods"] == ["squash", "rebase"] and pr["delete_branch_on_merge"]
    assert pr["head_sha"] == SHA and pr["merge_state"] == "CLEAN" and pr["author"] == "kurt"
    assert "headRefOid" in my_work.PR_QUERY and "squashMergeAllowed" in my_work.PR_QUERY


@pytest.mark.parametrize("action,params,expected,stdin", [
    ("ready", {}, ["pr", "ready", "7", "-R", "acme/app"], None),
    ("draft", {}, ["pr", "ready", "7", "-R", "acme/app", "--undo"], None),
    ("reviewers", {"reviewers": ["@byron", "acme/core", "byron"]},
     ["pr", "edit", "7", "-R", "acme/app", "--add-reviewer", "byron,acme/core"], None),
    ("comment", {"body": "LGTM\n`x`"}, ["pr", "comment", "7", "-R", "acme/app", "--body-file", "-"], "LGTM\n`x`"),
    ("update_branch", {}, ["pr", "update-branch", "7", "-R", "acme/app"], None),
    ("merge", {"method": "squash", "head_sha": SHA, "delete_branch": True},
     ["pr", "merge", "7", "-R", "acme/app", "--squash", "--match-head-commit", SHA, "--delete-branch"], None),
    ("merge", {"method": "rebase", "head_sha": SHA.upper()},
     ["pr", "merge", "7", "-R", "acme/app", "--rebase", "--match-head-commit", SHA], None),
])
def test_gh_commands(action, params, expected, stdin):
    args, body = work_actions.gh_command(action, "acme/app", 7, params)
    assert args == expected and body == stdin
    assert "--admin" not in args and "--auto" not in args


@pytest.mark.parametrize("action,params", [
    ("merge", {"method": "fast-forward", "head_sha": SHA}),
    ("merge", {"method": "squash"}),
    ("merge", {"method": "squash", "head_sha": "abc"}),
    ("reviewers", {"reviewers": []}),
    ("reviewers", {"reviewers": ["bad login;"]}),
    ("comment", {"body": " "}),
    ("close", {}),
])
def test_gh_command_rejects(action, params):
    with pytest.raises(ActionError):
        work_actions.gh_command(action, "acme/app", 7, params)


def test_pr_ref_validation():
    for repo, n in (("acme", 7), ("acme/app;x", 7), ("acme/app", 0), ("acme/app", "7"), ("acme/app", True)):
        with pytest.raises(ActionError):
            work_actions.check_pr_ref(repo, n)


@pytest.mark.asyncio
async def test_merge_runs_gh_and_drops_the_row(db):
    await store.upsert_source("github")
    await store.set_state(my_work.STATE_PLATFORM, "github", json.dumps({
        "items": [{"repo": "acme/app", "number": 7}, {"repo": "acme/app", "number": 8}],
        "fetched_at": "2026-11-19T00:00:00+00:00"}))
    reads = AsyncMock(side_effect=[_gh_read(), _gh_read(state="MERGED")])
    write = AsyncMock(return_value="merged")
    with patch.object(cli, "run_gh", reads), patch.object(cli, "run_gh_write", write):
        out = await work_actions.github_action("acme/app", 7, "merge",
                                               {"method": "squash", "head_sha": SHA, "delete_branch": True})
    assert write.await_args.args == ("pr", "merge", "7", "-R", "acme/app", "--squash",
                                     "--match-head-commit", SHA, "--delete-branch")
    assert out["removed"] and out["state"] == "MERGED"
    read_args = reads.await_args_list[0].args
    assert "owner=acme" in read_args and "name=app" in read_args and "number=7" in read_args
    cache = json.loads((await store.get_state(my_work.STATE_PLATFORM, "github"))["github"])
    assert [r["number"] for r in cache["items"]] == [8]


@pytest.mark.asyncio
async def test_comment_goes_through_stdin_and_refreshes_row(db):
    await store.upsert_source("github")
    await store.set_state(my_work.STATE_PLATFORM, "github", json.dumps({
        "items": [{"repo": "acme/app", "number": 7, "title": "old"}], "fetched_at": "x"}))
    write = AsyncMock(return_value="https://github.com/acme/app/pull/7#issuecomment-1")
    with patch.object(cli, "run_gh", AsyncMock(side_effect=[_gh_read(), _gh_read(title="new")])), \
         patch.object(cli, "run_gh_write", write):
        out = await work_actions.github_action("acme/app", 7, "comment", {"body": "--admin; $(rm -rf)"})
    assert write.await_args.kwargs["stdin"] == "--admin; $(rm -rf)"
    assert "--admin; $(rm -rf)" not in write.await_args.args
    assert out["pr"]["title"] == "new" and not out["removed"]
    cache = json.loads((await store.get_state(my_work.STATE_PLATFORM, "github"))["github"])
    assert cache["items"][0]["title"] == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("action,params,read,match,status", [
    ("ready", {}, {"viewer": "someone"}, "not your pull request", 403),
    ("ready", {}, {"author": {"login": "byron"}}, "not your pull request", 403),
    ("ready", {}, {}, "already ready", 409),
    ("draft", {}, {"isDraft": True}, "already a draft", 409),
    ("merge", {"method": "squash", "head_sha": SHA}, {"state": "CLOSED"}, "closed", 409),
    ("merge", {"method": "merge", "head_sha": SHA}, {}, "does not allow 'merge'", 400),
    ("merge", {"method": "squash", "head_sha": SHA}, {"headRefOid": "b" * 40}, "new commits", 409),
    ("merge", {"method": "squash", "head_sha": SHA}, {"isDraft": True}, "draft", 409),
    ("reviewers", {"reviewers": ["Kurt"]}, {}, "your own review", 400),
])
async def test_github_preconditions_refuse_before_writing(db, action, params, read, match, status):
    await store.upsert_source("github")
    viewer = read.pop("viewer", "kurt")
    write = AsyncMock()
    with patch.object(cli, "run_gh", AsyncMock(return_value=_gh_read(viewer=viewer, **read))), \
         patch.object(cli, "run_gh_write", write), pytest.raises(ActionError, match=match) as e:
        await work_actions.github_action("acme/app", 7, action, params)
    assert e.value.status == status
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_failure_is_reported_once(db):
    await store.upsert_source("github")
    write = AsyncMock(side_effect=cli.ClaudeCodeError("gh pr failed: Pull request is not mergeable"))
    with patch.object(cli, "run_gh", AsyncMock(return_value=_gh_read())), \
         patch.object(cli, "run_gh_write", write), pytest.raises(ActionError, match="not mergeable") as e:
        await work_actions.github_action("acme/app", 7, "update_branch")
    assert e.value.status == 502 and write.await_count == 1


@pytest.mark.asyncio
async def test_github_dry_run_and_not_connected(db):
    with pytest.raises(ActionError, match="not connected"):
        await work_actions.github_action("acme/app", 7, "ready")
    await store.upsert_source("github")
    write = AsyncMock()
    with patch.object(cli, "run_gh", AsyncMock(return_value=_gh_read(isDraft=True))), \
         patch.object(cli, "run_gh_write", write):
        out = await work_actions.github_action("acme/app", 7, "ready", dry_run=True)
    write.assert_not_awaited()
    assert out["command"] == ["gh", "pr", "ready", "7", "-R", "acme/app"]


# ── REST ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoints_map_errors_to_http(db):
    from laya.claude_code.api import router

    app = FastAPI()
    app.include_router(router)
    await store.upsert_source("github")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.post("/claude-code/my-work/github/action",
                              json={"repo": "acme/app", "number": 7, "action": "merge", "method": "squash"})
        assert r.status_code == 400 and "head commit" in r.json()["detail"]
        r = await client.post("/claude-code/my-work/linear/TRAD-1/action", json={"action": "assign_me"})
        assert r.status_code == 404
        r = await client.get("/claude-code/my-work/linear/states", params={"team": "bad key!"})
        assert r.status_code == 400
        with patch.object(cli, "run_gh", AsyncMock(return_value=_gh_read(isDraft=True))), \
             patch.object(cli, "run_gh_write", AsyncMock(return_value="")):
            r = await client.post("/claude-code/my-work/github/action",
                                  json={"repo": "acme/app", "number": 7, "action": "ready"})
        assert r.status_code == 200 and r.json()["ok"]
