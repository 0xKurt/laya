# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Prepare a coding session the user starts himself.

From a card this module writes (a) a session prompt in the user's style and
(b) the shell commands that give the session its own git worktree next to the
repository's checkout. Nothing is started: the user copies both
(docs/claude-code-connector.md, "Effort and session lane").

The worktree command has the form
``git -C <checkout> fetch origin && git -C <checkout> worktree add ../<repo>-<topic> -b <topic> origin/<default>``.
The checkout is a repos.json entry for the repository, else
``<root>/<repo>`` for the first configured checkout root that has it
(settings ``sessions.checkout_roots``, default the home folder); without
one a clone command comes first.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from laya.config import load_repos, load_settings
from laya.llm.prompts.overrides import get_prompt
from laya.models.card import CardResponse

log = structlog.get_logger()

# Where checkouts are looked for while settings ``sessions.checkout_roots`` is empty
# (Settings → Agent asks for the folder).
DEFAULT_CHECKOUT_ROOTS = ("~",)
GIT_TIMEOUT_SECONDS = 15
_TOPIC_MAX_CHARS = 40

_GITHUB_REPO_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[/#?\s)\]>]|$)")
_GITHUB_ENTITY = re.compile(r"^github:[a-z_]+:([\w.-]+/[\w.-]+)#\d+")
_LINEAR_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
# Untracked files a fresh worktree needs copied from the checkout.
_ENV_FILES = (".env", ".env.local", ".env.development.local", ".env.test.local")
# Lock file → install command run in a fresh worktree.
_INSTALLERS = (
    ("pnpm-lock.yaml", "pnpm install"),
    ("yarn.lock", "yarn install"),
    ("package-lock.json", "npm ci"),
    ("uv.lock", "uv sync"),
    ("foundry.toml", "forge install"),
)
# GitHub path segments that are not repositories (github.com/orgs/…, …/settings).
_NOT_OWNERS = {"orgs", "settings", "notifications", "marketplace", "apps", "users"}


SESSION_PREP_PROMPT = """\
You write the opening prompt for a coding session (Claude Code) that the user \
starts himself. The user is a senior engineer who steers several sessions and \
reviews their work.

Write it the way the user writes to a colleague: short, direct, plain, lowercase \
is fine, no greeting, no headings, no bullet lists unless there are several \
separate items. Cover, in this order:
1. what is wrong or what is wanted, and for whom;
2. where: every link, Linear key, PR, repository, contract or wallet address from \
the card, copied exactly and in full (never shorten an address or a hash);
3. what done looks like, concretely;
4. end with: "commit, open a PR, get it green. don't merge, wait for my word."
Do not invent facts the card does not state. If something needed is unknown, say \
what the session should find out first.

Also return `topic`: a git branch name for this work, lowercase words joined by \
"-", at most 40 characters, starting with the Linear key when there is one \
(e.g. "trad-590-owner-safe-handover")."""

_SCHEMA = {
    "name": "session_prep",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["topic", "prompt"],
        "additionalProperties": False,
    },
}

CLOSING_LINE = "commit, open a PR, get it green. don't merge, wait for my word."


@dataclass
class RepoTarget:
    """The repository a card's work happens in."""

    full_name: str | None  # owner/repo, when known
    name: str  # repo
    checkout: str | None  # local checkout path, when one exists
    default_branch: str = "main"
    # How the checkout was found: repos_config | checkout_root | none
    found_by: str = "none"


@dataclass
class SessionPrep:
    prompt: str
    topic: str
    repo: dict | None = None
    worktree_path: str | None = None
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── card text ──


def card_text(card: CardResponse) -> str:
    """The card's text as one block: header, summary, intelligence, staged output."""
    parts = [card.header or "", card.summary or ""]
    parts += [str(x) for x in (card.intelligence or [])]
    if card.staged_output and card.staged_output.content:
        parts.append(card.staged_output.content)
    return "\n".join(p for p in parts if p)


def card_links(card: CardResponse) -> list[str]:
    """Every URL of the card, source first, without duplicates."""
    urls = [card.source_url] if card.source_url else []
    urls += _URL.findall(card_text(card))
    return list(dict.fromkeys(u.rstrip(".,;:") for u in urls))


def linear_keys(card: CardResponse) -> list[str]:
    text = "\n".join([card.entity_id or "", card.source_ref or "", card_text(card)])
    return list(dict.fromkeys(_LINEAR_KEY.findall(text)))


# ── repository ──


def github_repo_of(card: CardResponse, metadata: dict | None = None) -> str | None:
    """owner/repo of the card's GitHub repository, when the card names one.

    Sources in order: a GitHub entity id, the event's `github_repo` metadata,
    the first github.com repository URL in the card.
    """
    m = _GITHUB_ENTITY.match(card.entity_id or "")
    if m:
        return m.group(1)
    repo = (metadata or {}).get("github_repo")
    if isinstance(repo, str) and "/" in repo:
        return repo.strip()
    for url in card_links(card):
        m = _GITHUB_REPO_URL.match(url)
        if m and m.group(1).lower() not in _NOT_OWNERS:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def _configured_repos() -> list[dict]:
    try:
        repos = load_repos().get("repos", [])
    except Exception as e:  # unreadable repos.json only costs the lookup
        log.warning("session_prep_repos_unreadable", error=str(e))
        return []
    return [r for r in repos if isinstance(r, dict)]


def _repo_from_config(card: CardResponse, full_name: str | None) -> dict | None:
    """The repos.json entry for `full_name`, or one whose name the card mentions."""
    repos = _configured_repos()
    if full_name:
        short = full_name.split("/")[-1].lower()
        for r in repos:
            if (r.get("remote_id") or "").lower() == full_name.lower():
                return r
        for r in repos:
            if (r.get("name") or "").lower() == short:
                return r
        return None
    text = card_text(card).lower()
    for r in repos:
        name = (r.get("name") or "").lower()
        if name and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            return r
    return None


def checkout_roots() -> list[Path]:
    sessions = load_settings().get("sessions", {})
    roots = sessions.get("checkout_roots") if isinstance(sessions, dict) else None
    if not isinstance(roots, list) or not roots:
        roots = list(DEFAULT_CHECKOUT_ROOTS)
    return [Path(str(r)).expanduser() for r in roots]


async def _run(*args: str) -> tuple[int, str]:
    """Run a command; (returncode, stdout). Tests replace this function."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, ""
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def default_branch(checkout: str | None, full_name: str | None) -> str:
    """origin's default branch: from the checkout, else from gh, else "main"."""
    if checkout:
        rc, out = await _run("git", "-C", checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if rc == 0 and out.startswith("origin/"):
            return out.removeprefix("origin/")
    if full_name and shutil.which("gh"):
        rc, out = await _run(
            "gh", "repo", "view", full_name, "--json", "defaultBranchRef",
            "-q", ".defaultBranchRef.name",
        )
        if rc == 0 and out:
            return out
    return "main"


async def resolve_repo(card: CardResponse, metadata: dict | None = None) -> RepoTarget | None:
    """The repository of the card's work, with its local checkout if there is one."""
    full_name = github_repo_of(card, metadata)
    config = _repo_from_config(card, full_name)
    if not full_name and config and "/" in (config.get("remote_id") or ""):
        full_name = config["remote_id"]
    if not full_name and not config:
        return None
    name = (full_name or config.get("name") or "").split("/")[-1]

    checkout, found_by = None, "none"
    if config and config.get("path") and Path(config["path"]).expanduser().is_dir():
        checkout, found_by = str(Path(config["path"]).expanduser()), "repos_config"
    else:
        for root in checkout_roots():
            candidate = root / name
            if (candidate / ".git").exists():
                checkout, found_by = str(candidate), "checkout_root"
                break
    return RepoTarget(
        full_name=full_name,
        name=name,
        checkout=checkout,
        default_branch=await default_branch(checkout, full_name),
        found_by=found_by,
    )


# ── topic and commands ──


def slugify_topic(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    slug = ""
    for w in words:
        nxt = f"{slug}-{w}" if slug else w
        if len(nxt) > _TOPIC_MAX_CHARS:
            break
        slug = nxt
    return slug or "session"


def fallback_topic(card: CardResponse) -> str:
    keys = linear_keys(card)
    prefix = f"{keys[0]} " if keys and keys[0].lower() not in (card.header or "").lower() else ""
    return slugify_topic(prefix + (card.header or ""))


async def _untracked_env_files(checkout: str) -> list[str]:
    found = []
    for name in _ENV_FILES:
        if not (Path(checkout) / name).is_file():
            continue
        rc, out = await _run("git", "-C", checkout, "ls-files", "--", name)
        if rc == 0 and not out:
            found.append(name)
    return found


async def worktree_commands(repo: RepoTarget, topic: str) -> tuple[str, list[str], list[str]]:
    """(worktree path, shell commands, notes) for a session on `topic` in `repo`."""
    q = shlex.quote
    notes: list[str] = []
    commands: list[str] = []
    checkout = repo.checkout
    if not checkout:
        root = checkout_roots()[0]
        checkout = str(root / repo.name)
        source = (
            f"git@github.com:{repo.full_name}.git" if repo.full_name else f"<clone url of {repo.name}>"
        )
        commands.append(f"git clone {q(source)} {q(checkout)}")
        notes.append(f"No local checkout of {repo.full_name or repo.name} found; clone it first.")
    worktree = str(Path(checkout).parent / f"{repo.name}-{topic}")
    commands.append(
        f"git -C {q(checkout)} fetch origin && "
        f"git -C {q(checkout)} worktree add ../{q(f'{repo.name}-{topic}')} "
        f"-b {q(topic)} origin/{q(repo.default_branch)}"
    )
    if repo.checkout:
        for env in await _untracked_env_files(repo.checkout):
            commands.append(f"cp {q(str(Path(repo.checkout) / env))} {q(worktree)}/")
        for lock, install in _INSTALLERS:
            if (Path(repo.checkout) / lock).is_file():
                commands.append(f"cd {q(worktree)} && {install}")
                break
    return worktree, commands, notes


# ── prompt ──


def _card_brief(card: CardResponse, repo: RepoTarget | None, worktree: str | None) -> str:
    lines = [
        f"Card: {card.header}",
        f"Summary: {card.summary}",
    ]
    if card.intelligence:
        lines.append("Findings:\n" + "\n".join(f"- {x}" for x in card.intelligence))
    if card.staged_output and card.staged_output.content:
        lines.append(f"Staged output ({card.staged_output.type}):\n{card.staged_output.content[:2000]}")
    if card.entity_id:
        lines.append(f"Work item: {card.entity_id}")
    keys = linear_keys(card)
    if keys:
        lines.append("Linear keys: " + ", ".join(keys))
    links = card_links(card)
    if links:
        lines.append("Links:\n" + "\n".join(links))
    if card.actor_name:
        lines.append(f"From: {card.actor_name}")
    if repo:
        lines.append(f"Repository: {repo.full_name or repo.name} (default branch {repo.default_branch})")
    if worktree:
        lines.append(f"The session runs in the worktree {worktree}.")
    return "\n".join(lines)


def template_prompt(card: CardResponse, worktree: str | None) -> str:
    """The prompt without the model: the card's facts in the user's order."""
    parts = [card.summary or card.header]
    where = card_links(card) + [k for k in linear_keys(card) if k not in " ".join(card_links(card))]
    if where:
        parts.append("where: " + " ".join(where))
    if worktree:
        parts.append(f"work in {worktree}.")
    parts.append(CLOSING_LINE)
    return "\n\n".join(parts)


def _with_closing_line(prompt: str) -> str:
    if "get it green" in prompt.lower():
        return prompt
    return prompt.rstrip() + "\n\n" + CLOSING_LINE


async def prepare_session(card: CardResponse, metadata: dict | None = None) -> SessionPrep:
    """Write the session prompt and the worktree commands for `card`.

    One LLM call (the stager's configured model) writes the prompt and the
    branch topic. When it fails, a template prompt from the card's facts is
    returned with `llm_error` set.
    """
    from laya.llm.client import llm_call

    repo = await resolve_repo(card, metadata)
    topic = fallback_topic(card)
    planned_worktree = None
    if repo:
        base = Path(repo.checkout) if repo.checkout else checkout_roots()[0] / repo.name
        planned_worktree = str(base.parent / f"{repo.name}-<topic>")

    prompt_text: str | None = None
    llm_error: str | None = None
    try:
        response = await llm_call(
            role="stager",
            messages=[
                {"role": "system", "content": get_prompt("session_prep", SESSION_PREP_PROMPT)},
                {"role": "user", "content": _card_brief(card, repo, planned_worktree)},
            ],
            response_schema=_SCHEMA,
            card_id=card.card_id,
            step="session_prep",
            temperature=0.2,
            max_tokens=1200,
            space_id=card.space_id,
        )
        data: Any = response.parsed or json.loads(response.content or "{}")
        if isinstance(data, dict):
            if isinstance(data.get("topic"), str) and data["topic"].strip():
                topic = slugify_topic(data["topic"])
            if isinstance(data.get("prompt"), str) and data["prompt"].strip():
                prompt_text = data["prompt"].strip()
        if not prompt_text:
            llm_error = "the model returned no prompt"
    except Exception as e:
        llm_error = str(e)
        log.warning("session_prep_llm_failed", card_id=card.card_id, error=str(e))

    worktree, commands, notes = (None, [], [])
    if repo:
        worktree, commands, notes = await worktree_commands(repo, topic)
        if planned_worktree and prompt_text:
            prompt_text = prompt_text.replace(planned_worktree, worktree)
    else:
        notes.append("No repository found on this card; add the repository to repos.json or the card.")

    prompt = _with_closing_line(prompt_text) if prompt_text else template_prompt(card, worktree)
    return SessionPrep(
        prompt=prompt,
        topic=topic,
        repo=asdict(repo) if repo else None,
        worktree_path=worktree,
        commands=commands,
        notes=notes,
        llm_error=llm_error,
    )
