# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""GET /claude-code/github/orgs: the orgs of the gh login and the GitHub source's scope."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from laya.claude_code import cli, store


@pytest.mark.asyncio
async def test_orgs_and_selected_scope(db):
    from laya.main import app

    await store.ensure_tables()
    await store.upsert_source("github", config={"orgs": ["acme"], "others_prs_only_on_mention": True})
    with patch.object(cli, "run_gh", AsyncMock(return_value=[{"login": "zeta"}, {"login": "acme"}])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/claude-code/github/orgs")).json()
    assert data == {"orgs": ["acme", "zeta"], "selected": ["acme"], "connected": True, "error": None}


@pytest.mark.asyncio
async def test_gh_failure_is_reported(db):
    from laya.main import app

    with patch.object(cli, "run_gh", AsyncMock(side_effect=cli.ClaudeCodeError("gh is not installed"))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/claude-code/github/orgs")).json()
    assert data["orgs"] == [] and data["error"] == "gh is not installed" and data["connected"] is False
