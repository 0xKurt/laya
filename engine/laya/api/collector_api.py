# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""MCP collector API — status and on-demand sweeps."""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from laya.collectors.mcp_collector import get_collector_status, run_sweep

router = APIRouter()


class SweepRequest(BaseModel):
    sources: Optional[list[str]] = None


@router.get("/collector/status")
async def collector_status() -> dict:
    return get_collector_status()


@router.post("/collector/sweep")
async def collector_sweep(body: SweepRequest | None = None) -> dict:
    """Run one sweep now (all enabled sources, or the ones named)."""
    return await run_sweep(body.sources if body else None)
