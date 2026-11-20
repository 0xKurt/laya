# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""GET /claude-code/gmail/senders and GET /claude-code/folders/check (settings pickers)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import store
from laya.db.timeutil import db_ts


async def _event(db, n, email, name="", rel="", platform="gmail", days_ago=1):
    ts = db_ts(datetime.now(timezone.utc) - timedelta(days=days_ago))
    await db.execute(
        """INSERT INTO events (event_id, timestamp, source_platform, source_raw_event_type,
                               actor_name, actor_email, actor_relationship, subject_type,
                               subject_id, raw_json)
           VALUES (?, ?, ?, 'new_email', ?, ?, ?, 'email_thread', ?, ?)""",
        (f"e{n}", ts, platform, name, email, rel, f"t{n}", json.dumps({})),
    )


async def _get(path):
    from laya.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_senders_counted_without_skipped_self_old_and_other_platforms(db):
    await store.ensure_tables()
    await store.upsert_source("gmail", config={"exclude_senders": ["skip@x.io"]})
    await _event(db, 1, "Ann@Corp.io", "Ann")
    await _event(db, 2, "ann@corp.io", "Ann")
    await _event(db, 3, "bob@corp.io", "Bob")
    await _event(db, 4, "skip@x.io")
    await _event(db, 5, "me@corp.io", rel="self")
    await _event(db, 6, "old@corp.io", days_ago=90)
    await _event(db, 7, "slack@corp.io", platform="slack")
    await _event(db, 8, "")
    await _event(db, 9, "gemini-notes@google.com", "Meeting notes")
    await _event(db, 10, "bob@corp.io", "Bob Builder", days_ago=0)
    await db.commit()

    data = await _get("/claude-code/gmail/senders")
    assert [(s["email"], s["count"]) for s in data["senders"]] == [("bob@corp.io", 2), ("ann@corp.io", 2)]
    assert data["senders"][0]["name"] == "Bob Builder" and data["senders"][1]["name"] == "Ann"
    assert data["skipped"] == ["skip@x.io"] and data["connected"] is True

    limited = await _get("/claude-code/gmail/senders?limit=1&days=365")
    assert [s["email"] for s in limited["senders"]] == ["bob@corp.io"]


@pytest.mark.asyncio
async def test_senders_without_gmail_source_use_default_skips(db):
    await store.ensure_tables()
    await _event(db, 1, "notifications@github.com")
    await _event(db, 2, "carol@corp.io")
    await db.commit()
    data = await _get("/claude-code/gmail/senders")
    assert [s["email"] for s in data["senders"]] == ["carol@corp.io"]
    assert data["connected"] is False


@pytest.mark.asyncio
async def test_folder_check(db, tmp_path):
    folder = tmp_path / "code"
    folder.mkdir()
    (tmp_path / "file.txt").write_text("x")

    ok = await _get(f"/claude-code/folders/check?path={folder}/")
    assert ok == {"path": str(folder), "exists": True, "error": None}

    missing = await _get(f"/claude-code/folders/check?path={tmp_path}/nope")
    assert missing["exists"] is False and "does not exist" in missing["error"]

    a_file = await _get(f"/claude-code/folders/check?path={tmp_path}/file.txt")
    assert a_file["exists"] is False and "file" in a_file["error"]

    empty = await _get("/claude-code/folders/check?path=")
    assert empty["exists"] is False


def test_folder_check_expands_home(monkeypatch, tmp_path):
    from laya.claude_code.pickers import check_folder

    monkeypatch.setenv("HOME", str(tmp_path))
    assert check_folder("~") == {"path": str(tmp_path), "exists": True, "error": None}


@pytest.mark.asyncio
async def test_github_reviewers_users_and_teams(db):
    from unittest.mock import AsyncMock, patch

    from laya.claude_code import cli

    async def fake(*args, **kw):
        if args[1].startswith("repos/acme/core/assignees"):
            return [{"login": "zed"}, {"login": "Ann"}]
        if args[1].startswith("orgs/acme/teams"):
            return [{"slug": "core"}]
        raise AssertionError(args)

    with patch.object(cli, "run_gh", AsyncMock(side_effect=fake)):
        data = await _get("/claude-code/my-work/github/reviewers?repo=acme/core")
    assert data == {"users": ["Ann", "zed"], "teams": ["acme/core"], "error": None}


@pytest.mark.asyncio
async def test_github_reviewers_failures(db):
    from unittest.mock import AsyncMock, patch

    from laya.claude_code import cli
    from laya.main import app

    with patch.object(cli, "run_gh", AsyncMock(side_effect=cli.ClaudeCodeError("gh is not installed"))):
        data = await _get("/claude-code/my-work/github/reviewers?repo=me/tool")
    assert data == {"users": [], "teams": [], "error": "gh is not installed"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/claude-code/my-work/github/reviewers?repo=not a repo")
    assert resp.status_code == 422
