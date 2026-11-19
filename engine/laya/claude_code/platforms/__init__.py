# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""One collector per platform: `async collect(source, since) -> list[LayaEvent]`.

`source` is the platform's `claude_code_sources` row as returned by
`store.get_source` (config merged over the defaults, discovered tools, the
user's identity), plus `deny_tools`: every discovered MCP tool, which the
collector turn denies except for its own read tools.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from laya.claude_code.platforms import github, gmail, linear, notion, slack
from laya.models.event import LayaEvent

Collector = Callable[[dict[str, Any], datetime], Awaitable[list[LayaEvent]]]

COLLECTORS: dict[str, Collector] = {
    "slack": slack.collect,
    "gmail": gmail.collect,
    "linear": linear.collect,
    "github": github.collect,
    "notion": notion.collect,
}
