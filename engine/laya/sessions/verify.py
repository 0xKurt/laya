# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Check what a coding session reports against GitHub.

A session's "merged" or "green" is a lead, not a fact (docs/claude-code-connector.md,
"Effort and session lane"). This module finds such claims about pull requests
in a text (a workspace agent's result, a card's summary), asks `gh` for the
pull request's state and checks, and labels each claim:

- ``confirmed``: gh shows the claimed state;
- ``contradicted``: gh shows another state;
- ``unverified``: gh could not be asked (no PR reference, no gh, an error), or
  the claim is one gh cannot check (``deployed``).

Only ``confirmed`` claims may be shown as facts.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import asdict, dataclass, field

import structlog

log = structlog.get_logger()

# At most this many pull requests are looked up for one text.
MAX_PRS = 5
GH_TIMEOUT_SECONDS = 20

_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_SHORT = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d+)\b")
_PR_NUMBER = re.compile(r"(?:\bPR\s*#?|(?<![\w/])#)(\d+)\b", re.IGNORECASE)

# Words that claim a state, per claim kind. Matched case-insensitively.
_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "merged": re.compile(r"\bmerged\b", re.IGNORECASE),
    "green": re.compile(
        r"\b(green|all checks pass(?:ed|ing)?|checks? (?:are |is )?pass(?:ed|ing)|ci (?:is )?pass(?:ed|ing))\b",
        re.IGNORECASE,
    ),
    "deployed": re.compile(r"\bdeployed\b", re.IGNORECASE),
}

_GREEN_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_FAILED_CONCLUSIONS = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


@dataclass
class PrClaim:
    """Claims made about one pull request (or about none, when `number` is None)."""

    repo: str | None
    number: int | None
    claims: list[str] = field(default_factory=list)
    # Filled by verify_claims.
    url: str | None = None
    state: str | None = None  # OPEN | MERGED | CLOSED
    checks: str | None = None  # green | failing | pending | none
    results: dict[str, str] = field(default_factory=dict)  # claim -> confirmed|contradicted|unverified
    error: str | None = None


def has_claim(text: str) -> bool:
    """Whether `text` claims any state a session can report."""
    return any(p.search(text or "") for p in _CLAIM_PATTERNS.values())


def _claims_in(text: str) -> list[str]:
    return [kind for kind, p in _CLAIM_PATTERNS.items() if p.search(text)]


def extract_claims(text: str, default_repo: str | None = None) -> list[PrClaim]:
    """Find state claims in `text`, grouped by the pull request they are about.

    The text is read line by line. A claim word on a line belongs to the pull
    requests referenced on the same line; a bare ``#12`` or ``PR 12`` needs
    `default_repo`. A claim on a line with no usable reference is kept with
    no pull request, so it is shown as unverified.
    """
    by_pr: dict[tuple[str | None, int | None], PrClaim] = {}
    for line in (text or "").splitlines():
        kinds = _claims_in(line)
        if not kinds:
            continue
        refs: list[tuple[str, int]] = []
        for m in _PR_URL.finditer(line):
            refs.append((m.group(1), int(m.group(2))))
        stripped = _PR_URL.sub(" ", line)
        for m in _PR_SHORT.finditer(stripped):
            refs.append((m.group(1), int(m.group(2))))
        if default_repo:
            for m in _PR_NUMBER.finditer(_PR_SHORT.sub(" ", stripped)):
                refs.append((default_repo, int(m.group(1))))
        keys: list[tuple[str | None, int | None]] = list(dict.fromkeys(refs)) or [(None, None)]
        for key in keys:
            claim = by_pr.setdefault(key, PrClaim(repo=key[0], number=key[1]))
            for kind in kinds:
                if kind not in claim.claims:
                    claim.claims.append(kind)
    return list(by_pr.values())


def checks_state(rollup: list[dict] | None) -> str:
    """Summarise gh's statusCheckRollup: green, failing, pending or none."""
    if not rollup:
        return "none"
    pending = False
    for item in rollup:
        # CheckRun items carry status + conclusion; StatusContext items carry state.
        conclusion = (item.get("conclusion") or item.get("state") or "").upper()
        status = (item.get("status") or "").upper()
        if conclusion in _FAILED_CONCLUSIONS:
            return "failing"
        if conclusion in _GREEN_CONCLUSIONS:
            continue
        if (status and status != "COMPLETED") or conclusion in {"PENDING", "EXPECTED", ""}:
            pending = True
    return "pending" if pending else "green"


async def _gh_pr_view(repo: str, number: int) -> dict:
    """`gh pr view` as JSON. Raises RuntimeError when gh is missing or fails."""
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("gh is not installed")
    proc = await asyncio.create_subprocess_exec(
        gh, "pr", "view", str(number), "--repo", repo,
        "--json", "state,mergedAt,url,statusCheckRollup",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("gh timed out")
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip()[:200] or "gh failed")
    return json.loads(out.decode())


async def verify_claims(text: str, default_repo: str | None = None) -> list[dict]:
    """Extract the claims in `text` and check each against gh."""
    claims = extract_claims(text, default_repo)
    looked_up = 0
    for claim in claims:
        claim.results = {kind: "unverified" for kind in claim.claims}
        if claim.number is None or claim.repo is None:
            continue
        if looked_up >= MAX_PRS:
            claim.error = "not checked: too many pull requests in one text"
            continue
        looked_up += 1
        try:
            data = await _gh_pr_view(claim.repo, claim.number)
        except Exception as e:  # gh missing, auth, network, unknown PR
            claim.error = str(e)
            log.info("session_claim_unverified", repo=claim.repo, number=claim.number, error=str(e))
            continue
        claim.url = data.get("url")
        claim.state = (data.get("state") or "").upper() or None
        claim.checks = checks_state(data.get("statusCheckRollup"))
        if "merged" in claim.results:
            claim.results["merged"] = "confirmed" if claim.state == "MERGED" else "contradicted"
        if "green" in claim.results:
            claim.results["green"] = {
                "green": "confirmed",
                "failing": "contradicted",
            }.get(claim.checks, "unverified")
    return [asdict(c) for c in claims]
