# Claude Code connector

Laya can collect Slack, Gmail, Linear, GitHub and Notion events through the user's **Claude Code CLI** instead of n8n: the MCP connectors the user's Claude Code login already has (claude.ai Slack/Gmail/Google Drive connectors, Linear and Notion MCP servers) and the `gh` CLI. No Slack app, OAuth client or n8n credential is needed. It can also send a Slack reply or a Gmail draft through the same connectors, only on the user's explicit click.

This document is the contract between the parts of the integration. Code that implements a part must match it; change the document together with the code.

## Design rules

1. **No migrations, no new columns on upstream tables.** `db/migrate.py` applies only versions above the highest applied one, so a fork migration would make a later upstream one be skipped. The module creates its own tables at startup with `CREATE TABLE IF NOT EXISTS` (`claude_code/store.py`).
2. **Use Laya's concepts.** Sources (`sources` rows), spaces, team identity (`team.json` `self`), entities (`event.entity_id`) and card status (`pipeline/card_status.py`, `entity_status_lock`), rules (`rules.json` via the rules API), tags (`tags` / `tag_assignments`, `is_system=1`), the agent budget (`pipeline/agent_budget.py`), audit (`log_to_audit`).
3. **Few hooks in upstream code.** Everything else lives in `engine/laya/claude_code/` and in new UI files. The hooks are listed in "Hooks" below.
4. **Reproducible.** Every setting is made through the setup wizard or Settings; defaults live in the module. No hand-edited files.
5. **Read-only collecting, explicit sending.** A collector turn gets an allowlist of read tools only. A send turn gets exactly one send tool and runs only from `POST /egress/execute` triggered by a user click.

## Engine module `engine/laya/claude_code/`

| File | Responsibility |
|---|---|
| `store.py` | tables `claude_code_sources` and `claude_code_state` (see below); async helpers; the per-platform config defaults |
| `discover.py` | locate the `claude` binary (`config.get_agent_binary("claude_code")`), list available MCP tools (one cheap `claude -p` listing turn, cached), map platform roles to real tool names, read the user's identity per platform; returns a structured `Discovery` with per-platform `available`, `tools`, `identity`, `problem` (human-readable reason when something is missing); `refresh_stored_tools` writes the newly discovered tool names into `claude_code_sources.tools` of every connected, available platform (on `POST /claude-code/discover` and on the listing a sweep runs when no discovery is cached) |
| `cli.py` | `run_collect(platform, prompt, schema, tools, model, timeout)` and `run_send(...)` on `agents/subprocess_helper.AgentProcess`; `--allowedTools` = the given tools, built-in tools disallowed; usage logged with `log_to_audit(step="claude_code_<platform>", model="agent/claude_code/<model>")` using cache reads weighted 0.1 |
| `platforms/slack.py`, `gmail.py`, `linear.py`, `notion.py`, `github.py` | one collector each: `async collect(source, since) -> list[LayaEvent]` |
| `platforms/common.py` | shared event helpers (`make_event`, stable ids, `split_conversation`) and the enrich step: `register_enricher(fn)` adds a step that runs over every sweep's events before they are posted (`links.py` registers here) |
| `links.py` | linked documents: extract Notion / Google Docs / GitHub links from an event, fetch read-only, summarise; appended to the event body (section "Linked documents" below) |
| `recheck.py` | "still open?" re-check of open cards |
| `service.py` | background loop, `start_service()` / `stop_service()` |
| `connect.py` | connect / disconnect / configure a platform |
| `egress.py` | `ClaudeCodeBackend(EgressBackend)` for sending |
| `my_work.py` | the "My work" view's data: open Linear issues and the user's own open pull requests (section "My work" below) |
| `api.py` | REST endpoints below |

### Tables (module-owned)

```sql
CREATE TABLE IF NOT EXISTS claude_code_sources (
    platform     TEXT PRIMARY KEY,          -- slack | gmail | linear | github | notion
    enabled      INTEGER NOT NULL DEFAULT 1,
    config       TEXT NOT NULL DEFAULT '{}', -- JSON, per-platform options (see Config)
    tools        TEXT NOT NULL DEFAULT '{}', -- JSON, role -> discovered tool name
    identity     TEXT NOT NULL DEFAULT '{}', -- JSON, e.g. {"slack_user_id": "U…", "email": "…"}
    cursor       TEXT,                       -- ISO timestamp, start of the last successful sweep
    last_run_at  TEXT, last_ok_at TEXT, last_error TEXT, last_count INTEGER,
    connected_at TEXT
);
CREATE TABLE IF NOT EXISTS claude_code_state (
    platform TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,  -- e.g. slack thread memory, links cache
    PRIMARY KEY (platform, key)
);
```

### Sources

On connect the module inserts (replacing a row auto-registered for the same workflow id), **before the first sweep**, a `sources` row: `source_id = "src_claude_code_<platform>"`, `name = "<Label> (Claude Code)"`, `platform = <platform>`, `workflow_id = "claude_code:<platform>"`, `source_type = "claude_code"`, `space_id = "default"` (or the chosen space). Every collected event has `source.connection_id = "claude_code:<platform>"`, so `space_resolution` maps it to that row and the n8n pause loops (which only touch `source_type='ingestion'`) ignore it. A space pause (`PUT /spaces/{id}/paused`) pauses the module's sources in that space.

### Event conventions

| Platform | `subject.type` | `subject.id` (= the conversation) | `actor.platform_handle` / `actor.email` |
|---|---|---|---|
| slack | `thread` | DM / group DM: `<channel_id>`; thread: `<channel_id>:<thread_root_ts>`; root message: `<channel_id>:<ts>` | Slack user id (`U…`) |
| gmail | `email_thread` | Gmail thread id | sender email |
| gmail meeting item | `meeting` | `<thread_id>:item:<n>` | email `gemini-notes@google.com` |
| linear | `ticket` | issue identifier (`TRAD-12`) | actor email |
| github | `pull_request` / `ticket` | `<owner>/<repo>#<number>` | GitHub login |
| notion | `page` | `<page_id>` or `<page_id>:<discussion_id>` | author email |

- `event_id = "evt_" + uuid5(NAMESPACE_URL, "laya-claude-code:<platform>:<native id>")`. Overlapping sweeps re-deliver the same ids and `POST /events` drops duplicates.
- At most two events per conversation and sweep: the user's latest own message (it concludes what came before it), and one event carrying all new messages after it. When the user wrote last, only the user's message is emitted. Both are needed when the user answered and someone wrote again in the same sweep: without the user's event the cards of earlier sweeps would stay open.
- `content.body` for Slack conversations: `Earlier in the conversation:` with up to `context_messages` previous messages, then `New:`. Each line is `[<Mon dd.mm HH:MM>] <Name|Me>: <text>`. Earlier messages are read only for a conversation with a new message from someone else that no earlier sweep delivered (`seen:` state, kept `thread_memory_days`) and that has earlier messages (a DM or a thread reply); otherwise the earlier sweeps' events carry them.
- Slack titles: `DM with <name>`, `Group DM: <other members>` (without the user's own name), `#<channel>`. A group DM is recognised by its reported type or by more than two participants, also when its channel id starts with `D`; only then does the `D` prefix mean a 1:1 DM.
- **Identity contract.** `connect.py` writes the user's handles into team.json `self.accounts` (Slack user id, GitHub login) and emails into `self.aliases`, through the team API. Collectors emit exactly those strings, so `run_ingest` yields `actor_relationship == "self"` for the user's own messages.
- **Conclusions.** An event with `content.metadata.resolved_in_source = true` (Linear issue Done/Canceled, PR merged or closed, re-check found the item answered; `raw_event_type` `issue_status_changed`, `pr_merged`, `pr_closed`, `recheck_resolved`) concludes its conversation like the user's own reply. `raw_event_type` values the seeded rules match: `review_requested`, `state_change`, `ci_activity`.
- `content.metadata` always has `claude_code: true` and the platform's own keys (`slack_channel_id`, `slack_channel` (the same channel id, the key Slack egress reads), `slack_channel_name`, `slack_channel_type`, `slack_thread_ts`, `gmail_thread_id`, `gmail_message_id` (the newest message of the event; drafts are threaded by it), `github_repo`, `github_reason`, `linear_status`, `notion_page_id`, `meeting`, `linked_documents` (the links summarised in the body)).

### Collector turns

Each collector turn asks the model to make its independent tool calls in parallel in one step (every extra round re-reads the whole conversation) and to answer in compact field names (e.g. Slack `c`, `ts`, `th`, `u`, `n`, `t`) and the code maps them, builds permalinks / web links (Slack from the workspace subdomain, Gmail `https://mail.google.com/mail/?authuser=<email>#all/<thread id>`, Linear from the workspace slug, Notion `https://www.notion.so/<page id>`), groups, filters and titles. The prompts set hard caps:

| Platform | Turns and caps |
|---|---|
| slack | search turn: three searches in parallel, `with:<@me>` (DMs, group DMs and threads the user is in), keyword `<@me>` with `-from:<@me>` (mentions), `from:<@me>`; `response_format=detailed`, `include_context=false`, 20 results, a second page only after a full first page; no thread or channel reads; of the user's own messages only the newest per conversation. Context turn only when needed (see above): all reads in parallel, at most 8 conversations, `read_channel` with `limit=context_messages`, `read_thread` with `limit=50`. Text at most 1500 characters (own messages 500, context 600) |
| gmail | `search_threads` `THREAD_VIEW_MINIMAL`, 20 threads, at most 2 pages; own mails keep their snippet; mails from others are read one by one (`get_message`, else `get_thread`), at most 15, body = newest part only, at most 1200 characters |
| notion | own pages and pages naming the user, 10 each, edited since the window's day; comments of at most 12 pages; mentions from the search highlight (300 characters), no page fetches; text at most 1500 characters |
| linear | notifications since the window; assigned issues `limit=25`; comment text at most 1500 characters, descriptions 500 |

### Config (per platform, `claude_code_sources.config`)

| Platform | Keys (defaults) |
|---|---|
| all | `interval_minutes` 60, `active_hours` {start "08:00", end "19:00", days [0..4]}, `timezone` (system), `model` "haiku", `initial_lookback_hours` 12, `recheck_after_hours` 6 |
| slack | `context_messages` 8, `thread_memory_days` 14, `only_relevant` true (DMs, mentions, own messages, threads the user wrote in), `linked_documents` true |
| gmail | `exclude_senders` [notifications@linear.app, notifications@github.com, noreply@github.com, notify@mail.notion.com], `meeting_notes` true, `linked_documents` true |
| github | `orgs` [] (empty = all), `others_prs_only_on_mention` true |
| linear, notion | `linked_documents` true |

### Linked documents (`links.py`)

An enricher (`register_enricher`) over the Slack, Gmail, Linear and Notion events of every sweep, switched per platform by `linked_documents`. It finds links in `content.body` to Notion pages (`notion.so` / `notion.site`, page id), Google Docs / Sheets / Slides / Drive files (file id) and GitHub issues / pull requests, skipping the event's own `subject.url`, and appends

```
Linked documents:
- <title> (<link>): <2-4 sentence summary or the relevant excerpt>
```

- At most 3 links per event, in order of appearance, one entry per document.
- GitHub: `gh api repos/<owner>/<repo>/issues/<n>`, no model turn; the entry is the state (open / closed / merged) and the start of the description.
- Notion and Google: one batched `claude -p` turn per event (step `claude_code_links`, the platform's `model`) that may only use the discovered Notion fetch tool and the Drive read tool (`drive_read`); every other MCP tool is denied.
- Every result, also "could not be read", is cached in `claude_code_state` (platform `links`, key `link:<kind>:<id>`) for 24 h; a failed turn caches nothing.
- While the agent budget is paused no model turn runs; cached entries and GitHub entries are still added.
- A failure leaves the event as it was.

### REST API (`api.py`, auth like every router)

| Method | Path | Body / result |
|---|---|---|
| GET | `/claude-code/status` | `{cli: {found, path, version}, platforms: {<p>: {available, connected, enabled, identity, problem, last_run_at, last_ok_at, last_error, last_count, config, can_send}}, budget_paused: bool}` |
| POST | `/claude-code/discover` | runs discovery again, returns status |
| POST | `/claude-code/{platform}/connect` | `{space_id?}` → creates the source, identity and rules, returns the platform status |
| DELETE | `/claude-code/{platform}` | disconnects: source row, module rows, the platform's seeded rules |
| PUT | `/claude-code/{platform}/config` | the full config object for the platform |
| PUT | `/claude-code/{platform}/enabled` | `{enabled: bool}` → pauses or resumes the platform's sweeps, returns the platform status |
| POST | `/claude-code/{platform}/sync` | sweeps now, returns `{events, error}` |
| GET | `/claude-code/my-work?refresh=false` | `{github: {prs, connected, error, note, fetched_at, cached}, linear: {issues, …same}}` (section "My work") |

### Seeded rules (idempotent by name, through the rules API)

- `Claude Code · GitHub: ignore review requests, state changes and CI`: drop when `source.platform equals github` and `source.raw_event_type in [review_requested, state_change, ci_activity]`.
- `Claude Code · Ignore bot comments`: drop when `actor.platform_handle ends_with [bot]`.
- `Claude Code · Linear: ignore review requests`: drop when `source.platform equals linear` and `source.raw_event_type equals review_requested`.

## Hooks in upstream code

| File | Hook |
|---|---|
| `main.py` | include `claude_code.api.router`; `start_service()` in lifespan, `stop_service()` on shutdown |
| `pipeline/queue.py` | `_fetch_ready_events`: return nothing while the agent budget is paused. `process_event`: after ingest and space resolution, `if actor_relationship == "self" or event.content.metadata.get("resolved_in_source"): await apply_conclusion(event, actor_relationship); mark filtered; return` (before the rules, so a conclusion is never dropped by a rule). `_batch_route_events` skips the same events. |
| `pipeline/card_status.py` | `apply_conclusion(event)`: under `entity_status_lock(event.entity_id)` close the open cards (`ready`, `requires_approval`) of the entity whose event is at or before this one (`close_earlier_cards` with a `statuses` argument; the stager path keeps `ready` only), with `status_set_by='laya'` and a reason with the event's URL, and record the conclusion so a card created later for an earlier event of the same entity is created `done` (`_later_conclusion_reason`). The conclusion is recorded on the event's own row: `events.filter_rule` holds the reason (`You replied in this conversation…` / `Resolved in the source…`), so no new column or table is needed; `decide_card_status` applies it to late `ready` and `requires_approval` cards |
| `egress/router.py` | `_resolve_backend`: a connection whose id starts with `claude_code:` → `ClaudeCodeBackend` |
| `egress/health.py`, `egress/connections.py` (`test_connection`, `remove_connection`) | rows with ids starting `claude_code:` are delegated to the module |
| `egress/tool_handlers.py` | `_resolve_connection_id`: a `claude_code:` row only when the platform has no other connected row |
| `claude_code/api.py` | imports `claude_code.egress`, which registers the connect/disconnect hooks |
| UI | `IntegrationsConfig.svelte` injects a "via Claude Code" entry per platform (the SMTP injection pattern) using `/claude-code/status`; `ClaudeCodeSource.svelte` is the detail panel; the setup wizard's step 3 includes `SetupClaudeCode.svelte`; `RulesEditor.svelte` accepts a free field path |

## Sending (Claude Code egress)

`connect.py` creates (through a hook `egress.py` registers in `connect.CONNECT_HOOKS`, removed through `DISCONNECT_HOOKS`), for platforms that can send (`slack`, `gmail`), an `egress_connections` row with `connection_id = "claude_code:<platform>"`, `n8n_credential_id` NULL, `status` from discovery, capabilities `["reply_thread","send_message"]` (Slack, the Slack platform contract's action types) / `["create_draft","reply"]` (Gmail; the backend also accepts the contract's `send_email` and turns it into a draft). `ClaudeCodeBackend.execute` runs one `claude -p` turn with exactly the one send tool needed for the action (Slack: the reply/send tool; Gmail: create a **draft**, never send) and the payload from the card. It runs only through the egress execute path (`POST /egress/execute`, or the card executor via `/actions/execute` and the workspace `execute_action` message), all started by the user's click; chat tools only preview (`confirm_egress` is refused) and processing rules skip `execute_egress`.

Routing (`egress/router._resolve_backend`): a request naming `claude_code:<platform>` goes to `ClaudeCodeBackend`; a request without a connection goes there only when its source event was collected through Claude Code (`events.source_connection_id = claude_code:<platform>`), the send connection exists and the action is one the backend performs. Requests naming any other connection, and events ingested by n8n, stay with n8n. Chat's connection pick (`tool_handlers._resolve_connection_id`) takes the Claude Code row only when the platform has no other connected row.

A send turn: `--allowedTools` = the one tool (Slack `send` role, Gmail `draft` role, which must be `create_draft`), `--disallowedTools` = every other discovered MCP tool, `--tools ""`; the prompt holds the arguments as JSON to copy verbatim (Slack `channel_id`/`message`/`thread_ts` for `reply_thread`; Gmail `to`/`cc`/`bcc`/`subject`/`body`/`threadId`); `--json-schema` asks for `{ok, permalink, ts, error}` (Slack) / `{ok, draft_id, thread_id, url, error}` (Gmail). A result without `ok`, without a link/ts or draft id, or with permission denials is a failure; failures are not retryable because the tool may have run. Health, `test_connection` (fresh discovery) and `remove_connection` (removes only the send row; collecting stays) are delegated to `claude_code/egress.py`.

## My work

`/work` (`ui/src/routes/work/+page.svelte`, pure logic in `ui/src/lib/work/work.ts`) shows the user's open Linear issues and open GitHub pull requests from `GET /claude-code/my-work` (`claude_code/my_work.py`).

- GitHub, only while the github source is connected: the pull requests the user authored (`is:pr is:open author:@me archived:false`, plus `org:<o>` per configured org; no review requests, no other people's PRs), read with one `gh api graphql` search per 25 PRs (at most 4 pages): repository, draft, base and head branch, mergeable state, review decision, requested reviewers, latest reviews, the status-check rollup with failing / running counts, unresolved review threads, and the first Linear key in the title or branch (`TRAD-123`; `ERC-20`-style words are skipped).
- Linear, only while the linear source is connected: one `claude -p` turn (step `claude_code_linear_my_work`, the source's `model`) that may call only the discovered `list_issues` tool, every other MCP tool denied. It lists the issues assigned to the user and the issues the user created in the open state types (`started`, `unstarted`, `backlog`, `triage`); the code merges them into one row per issue with `assigned_to_me` / `created_by_me`, drops completed / canceled ones, and keeps the assignee. Cycle names are not read (`list_issues` returns only a cycle id).
- Each section is cached in `claude_code_state` (platform `my_work`, keys `github` / `linear`) for 10 minutes; `refresh=true` skips the cache. While the agent budget is paused the Linear turn does not run and the cached issues come back with a `note`. A failure is reported in the section's `error` next to its last cached data; the endpoint does not fail.
- UI: Linear grouped In progress → Todo → Backlog → Triage, sorted by priority, due date, update; filter "Assigned to me" · "Created by me" · "All" (default assigned, `localStorage` `laya.work.linearFilter`); issues the user created for someone else show the assignee or "Unassigned". Pull requests grouped Needs your action (changes requested, failing checks, conflicts) → Waiting on review → Ready to merge (approved and green) → Drafts.

## Board

`/board` lanes come from Laya's own data: **In progress** = status `agent_running` / `awaiting_input` / `executing`, or system tag `lane:in-progress`; **To do** = system tag `lane:todo`; **Done** = status `done` (last 7 days); **Backlog** = every other active card. Dragging changes the card status (`/cards/{id}/done`, `/reopen`, `/archive`) and the lane tags (`/tags/assign`, `/tags/unassign`). The lane tags are created as system tags on first use.

## Effort and session lane

- Effort: the stager also returns `effort: {you_minutes, agent_minutes, basis}` (Kurt's time: deciding, reviewing, talking; agent time: code, tests, write-ups a Claude session can do). It is stored in the card's `staged_output` and shown on the card; the Board shows the sum of Kurt's time per lane.
- "Prepare session": a card action that produces a prompt in Kurt's style (what is wrong, for whom, where with links/addresses/Linear keys, what done looks like, "commit, open a PR, get it green"; merging waits for Kurt) plus a worktree command for the repository (`git -C <checkout> worktree add ../<repo>-<topic> -b <topic> origin/<default>`). Kurt copies it and starts the session himself.
- A session's claim is a lead: a card shows "merged" / "PR green" only after `gh` confirms it.

Implementation:

| Part | Where |
|---|---|
| Effort | `sessions/effort.py`; prompt block `EFFORT_RULES` + schema property in `llm/prompts/stager.py`, sent only while settings `pipeline.effort_estimate` is on (default). Stored as `staged_output.effort = {you_minutes, agent_minutes, basis, set_by}` (`set_by` = `stager` or `user`); copied to `CardResponse.effort` on every payload, the slim list included. A workspace agent's result keeps it (`staged_output_keeping_effort`). |
| Correction | `PUT /cards/{id}/effort` `{you_minutes, agent_minutes, basis?}`; broadcasts `card_updated` with `{effort}`. |
| Prepare session | `POST /cards/{id}/session-prep` → `{prompt, topic, repo, worktree_path, commands, notes, llm_error}` (`sessions/prep.py`). One LLM call on the stager's model (override key `session_prep`); without it a template prompt from the card. Repository: GitHub entity id, event `github_repo`, a github.com URL on the card, or a `repos.json` entry the card names. Checkout: the `repos.json` path, else `<root>/<repo>` under settings `sessions.checkout_roots` (default `~/git/somnia`), else a clone command first. After the worktree command: `cp` of untracked env files and the install command of the checkout's lock file. |
| Claims | `GET /cards/{id}/session-claims` (`sessions/verify.py`): merged / green / deployed claims in the card's text, grouped per PR, checked with `gh pr view --json state,statusCheckRollup`; each `confirmed`, `contradicted` or `unverified`. The UI (`SessionClaims.svelte`, card detail and workspace) shows only confirmed claims as facts. |

