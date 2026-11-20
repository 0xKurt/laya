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
| `cli.py` | `run_collect(platform, prompt, schema, tools, model, timeout, deny, env, account_id)` and `run_send(...)` on `agents/subprocess_helper.AgentProcess`; `--allowedTools` = the given tools, built-in tools disallowed; usage logged with `log_to_audit(step="claude_code_<platform>", model="agent/claude_code/<model>")` using cache reads weighted 0.1; the rate-limit signal is recorded for `account_id` (the account whose `env` the turn ran with) |
| `platforms/slack.py`, `gmail.py`, `linear.py`, `notion.py`, `github.py` | one collector each: `async collect(source, since) -> list[LayaEvent]` |
| `platforms/common.py` | shared event helpers (`make_event`, stable ids, `split_conversation`) and the enrich step: `register_enricher(fn)` adds a step that runs over every sweep's events before they are posted (`links.py` registers here) |
| `links.py` | linked documents: extract Notion / Google Docs / GitHub links from an event, fetch read-only, summarise; appended to the event body (section "Linked documents" below) |
| `recheck.py` | "still open?" re-check of open cards |
| `service.py` | background loops (sweeps and usage polls; My work refreshes), `start_service()` / `stop_service()` |
| `connect.py` | connect / disconnect / configure a platform |
| `egress.py` | `ClaudeCodeBackend(EgressBackend)` for sending |
| `my_work.py` | the "My work" view's data: open Linear issues and the user's own open pull requests (section "My work" below) |
| `work_actions.py` | direct actions on My work rows, each on the user's click (section "My work actions" below) |
| `terminal.py` | the Claude terminal: plain `claude` sessions in a PTY (section "Claude terminal" below) |
| `accounts.py` | Claude accounts: registry, routing of inference stages and chat, per-platform account gating for collectors, My work, actions and sending, per-account usage (rate-limit signal and `/usage` poll), sign-in in a PTY, connector matrix (section "Claude accounts" below); `login_module(agent_id)` gives the logins module of an agent |
| `codex_accounts.py` | Codex logins: the same registry / routing / usage for `codex_cli` (section "Codex logins" below) |
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
CREATE TABLE IF NOT EXISTS claude_code_accounts (        -- accounts.py; the CLI login has no row
    id TEXT PRIMARY KEY, config_dir TEXT NOT NULL,         -- ~/.laya/claude-accounts/<id>/ (0700)
    email TEXT, plan TEXT, org_name TEXT, logged_in INTEGER NOT NULL DEFAULT 0,
    status_checked_at TEXT, created_at TEXT NOT NULL,
    org_id TEXT,                                           -- added in place by accounts._db() on older tables
    agent_id TEXT NOT NULL DEFAULT 'claude_code'           -- claude_code | codex_cli; added in place likewise
);
CREATE TABLE IF NOT EXISTS claude_code_terminal_sessions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, cwd TEXT NOT NULL,
    claude_session_id TEXT NOT NULL,        -- passed as --session-id, resumed with --resume
    created_at TEXT NOT NULL, last_active_at TEXT, closed_at TEXT
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
| GET | `/claude-code/my-work?refresh=false` | `{github: {prs, connected, error, note, fetched_at, attempted_at, cached, refreshing}, linear: {issues, …same}}`, at once; stale sections refresh in the background (section "My work") |
| GET | `/claude-code/my-work/linear/states?team=TRAD&refresh=false` | `{team, states: [{id, name, type}]}` |
| POST | `/claude-code/my-work/linear/{identifier}/action` | `{action: state\|assign_me\|priority\|comment, value, dry_run?}` → `{ok, issue, removed, result}` |
| POST | `/claude-code/my-work/github/action` | `{repo, number, action: ready\|draft\|reviewers\|comment\|update_branch\|merge, reviewers?, body?, method?, head_sha?, delete_branch?, dry_run?}` → `{ok, pr, removed, state, output}` |
| GET | `/claude-code/terminal/sessions` | `{sessions: [<open session>], closed: [<row + resumable>], default_cwd}` |
| POST | `/claude-code/terminal/sessions` | `{cwd?, name?, resume?, cols?, rows?}` → the new session (`resume` = id of a closed session) |
| PATCH | `/claude-code/terminal/sessions/{id}` | `{name}` → the session |
| DELETE | `/claude-code/terminal/sessions/{id}` | closes the session (process group killed, MCP token revoked) |
| POST | `/claude-code/terminal/{id}/resize` | `{cols, rows}` |
| POST | `/claude-code/terminal/{id}/input` | `{text}` → inserted as one bracketed paste, control characters stripped, no Enter |
| WS | `/claude-code/terminal/{id}/pty` | PTY stream (section "Claude terminal") |
| GET | `/claude-code/accounts?refresh=false` | `{accounts: [{id, default, email, plan, logged_in, in_use, pending, problem, usage: {windows, as_of, models}, connectors}], current, thresholds, login, connectors_url}`; `refresh=true` polls every account's `/usage` first |
| GET | `/claude-code/accounts/login` | `{login}`: the latest sign-in (account or connector, running or ended) or null; the UI renders the sign-in panel from it on every poll |
| POST | `/claude-code/accounts` | starts signing in a new account → the sign-in `{id, kind, account_id, new_account, server, platform, state, url, error, started_at, expires_at}` |
| POST | `/claude-code/accounts/{id}/login` | signs an added account in again → the sign-in |
| POST | `/claude-code/accounts/{id}/code` | `{code}` → written to the waiting CLI; never logged or returned |
| GET | `/claude-code/accounts/{id}/status` | `{login}` (poll while `state` is `starting` / `waiting_code` / `verifying`) |
| POST | `/claude-code/accounts/{id}/cancel` | ends the sign-in |
| DELETE | `/claude-code/accounts/{id}` | `claude mcp logout` per server, `claude auth logout`, deletes the directory and the rows |
| POST | `/claude-code/accounts/{id}/connectors/check` | lists the account's connectors (init message, no model call) → the matrix |
| POST | `/claude-code/accounts/{id}/connectors/{linear\|notion}/add` | registers the default login's server (type and URL only) in the account |
| POST | `/claude-code/accounts/{id}/connectors/{linear\|notion}/login` | `claude mcp login <server> --no-browser` in a PTY → the sign-in |

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
| `security/engine_auth.py` | `require_engine_auth` authenticates a websocket route on a REST router with `require_ws_engine_auth` (the terminal's `/pty` socket) |
| `mcp/turn_tokens.py` | `mint(..., ttl_seconds=None)` mints a token without expiry (a terminal session's, revoked on close) |
| `llm/agent_backend.py` | Claude accounts: `_run_agent` takes the account's environment and records its rate-limit signal (per account) before raising; `_run_agent_routed` picks the account and retries once after a rejection; `agent_llm_stream` picks the account and retries once before anything was yielded |
| `llm/client.py` | the agent path adds `claude_account` to the audit metadata and leaves recording Claude Code's rate limit to `agent_backend` |
| `pipeline/agent_budget.py` | `rate_limit_windows` / `get_rate_limit_raw`; `evaluate_agent_budget` asks `accounts.native_block` for `claude_code`; `maybe_resume_agent_budget` ends an accounts-only pause once an account has room |
| UI | `IntegrationsConfig.svelte` injects a "via Claude Code" entry per platform (the SMTP injection pattern) using `/claude-code/status`; `ClaudeCodeSource.svelte` is the detail panel; the setup wizard's step 3 includes `SetupClaudeCode.svelte`; `RulesEditor.svelte` accepts a free field path |

## Sending (Claude Code egress)

`connect.py` creates (through a hook `egress.py` registers in `connect.CONNECT_HOOKS`, removed through `DISCONNECT_HOOKS`), for platforms that can send (`slack`, `gmail`), an `egress_connections` row with `connection_id = "claude_code:<platform>"`, `n8n_credential_id` NULL, `status` from discovery, capabilities `["reply_thread","send_message"]` (Slack, the Slack platform contract's action types) / `["create_draft","reply"]` (Gmail; the backend also accepts the contract's `send_email` and turns it into a draft). `ClaudeCodeBackend.execute` runs one `claude -p` turn with exactly the one send tool needed for the action (Slack: the reply/send tool; Gmail: create a **draft**, never send) and the payload from the card. It runs only through the egress execute path (`POST /egress/execute`, or the card executor via `/actions/execute` and the workspace `execute_action` message), all started by the user's click; chat tools only preview (`confirm_egress` is refused) and processing rules skip `execute_egress`.

Routing (`egress/router._resolve_backend`): a request naming `claude_code:<platform>` goes to `ClaudeCodeBackend`; a request without a connection goes there only when its source event was collected through Claude Code (`events.source_connection_id = claude_code:<platform>`), the send connection exists and the action is one the backend performs. Requests naming any other connection, and events ingested by n8n, stay with n8n. Chat's connection pick (`tool_handlers._resolve_connection_id`) takes the Claude Code row only when the platform has no other connected row.

A send turn: `--allowedTools` = the one tool (Slack `send` role, Gmail `draft` role, which must be `create_draft`), `--disallowedTools` = every other discovered MCP tool, `--tools ""`; the prompt holds the arguments as JSON to copy verbatim (Slack `channel_id`/`message`/`thread_ts` for `reply_thread`; Gmail `to`/`cc`/`bcc`/`subject`/`body`/`threadId`); `--json-schema` asks for `{ok, permalink, ts, error}` (Slack) / `{ok, draft_id, thread_id, url, error}` (Gmail). A result without `ok`, without a link/ts or draft id, or with permission denials is a failure; failures are not retryable because the tool may have run. Health, `test_connection` (fresh discovery) and `remove_connection` (removes only the send row; collecting stays) are delegated to `claude_code/egress.py`.

## My work

`/work` (`ui/src/routes/work/+page.svelte`, pure logic in `ui/src/lib/work/work.ts`) shows the user's open Linear issues and open GitHub pull requests from `GET /claude-code/my-work` (`claude_code/my_work.py`).

- GitHub, only while the github source is connected: the pull requests the user authored (`is:pr is:open author:@me archived:false`, plus `org:<o>` per configured org; no review requests, no other people's PRs), read with one `gh api graphql` search per 25 PRs (at most 4 pages): repository, draft, base and head branch, mergeable state, review decision, requested reviewers, latest reviews, the status-check rollup with failing / running counts, unresolved review threads, and the first Linear key in the title or branch (`TRAD-123`; `ERC-20`-style words are skipped).
- Linear, only while the linear source is connected: one `claude -p` turn (step `claude_code_linear_my_work`, the source's `model`) that may call only the discovered `list_issues` tool, every other MCP tool denied. It lists the issues assigned to the user and the issues the user created in the open state types (`started`, `unstarted`, `backlog`, `triage`); the code merges them into one row per issue with `assigned_to_me` / `created_by_me`, drops completed / canceled ones, and keeps the assignee. Cycle names are not read (`list_issues` returns only a cycle id).
- Each section is stored in `claude_code_state` (platform `my_work`, keys `github` / `linear`) as `{items, fetched_at, attempted_at, error}`: the last success, the last try, and the last try's failure with its reason (`null` after a success; e.g. a timeout, "Weekly limit reached on <account>; resets …" read from the account's usage windows before the turn, or "No Claude account has Linear connected (<account>: <reason>)").
- Refresh runs only in the background (`my_work.start_refresh`, one task per section, the whole fetch under `SECTION_TIMEOUT`: GitHub 180 s, Linear 420 s, and the `claude` / `gh` process is killed when it is cancelled). A second loop in `service.py` starts every minute the refresh of each connected section whose last try is older than its interval (GitHub 10 min, Linear 30 min, the Linear one only while the agent budget is not paused), inside the source's `active_hours`. The section lock covers only writing the result, so a hung refresh never blocks a read or an action.
- `GET /claude-code/my-work` never waits for a fetch: it returns the stored sections at once with `fetched_at`, `attempted_at`, `error`, `note` and `refreshing`, and starts a refresh of a stale section (any section with `refresh=true`). A finished refresh broadcasts `{"type": "my_work_updated", "section"}` over `/ws`. While the agent budget is paused the Linear turn does not run and the section carries a `note`.
- After a My work action the row's fresh data (read back by the action) replaces it in the stored section and is broadcast the same way; a refresh running at that moment keeps the action's row.
- UI: the stored data shows at once; each section shows "Updated x ago" or a spinner while it refreshes, the error with its reason above the last data, and the page re-reads on `my_work_updated` (and every 4 s while a section refreshes). Linear grouped In progress → Todo → Backlog → Triage, sorted by priority, due date, update; filter "Assigned to me" · "Created by me" · "All" (default assigned, `localStorage` `laya.work.linearFilter`); issues the user created for someone else show the assignee or "Unassigned". Pull requests grouped Needs your action (changes requested, failing checks, conflicts) → Waiting on review → Ready to merge (approved and green) → Drafts.

## My work actions

`claude_code/work_actions.py`, endpoints above. Every action runs only from its endpoint, which the UI calls on the user's click; nothing runs on a schedule and nothing is retried. Refusals and failures come back as HTTP errors with a readable `detail` (400 invalid input, 403 not the user's PR, 404 not connected, 409 the row changed or an action on it is already running, 502 the tool or `gh` failed). `dry_run: true` validates and returns the command without running it.

- **Linear** (change state, assign to me, set priority, comment). Discovery maps the roles `save_issue`, `save_comment` (`discover.WRITE_ROLES`, never in `READ_ROLES`) and `list_issue_statuses`. An action is one write turn whose `--allowedTools` is exactly that tool, every other discovered MCP tool in `--disallowedTools`, `--tools ""`; the prompt holds the arguments as JSON to copy verbatim: `save_issue` `{id, state | assignee: "me" | priority}` (always `id`, which makes it an update), `save_comment` `{issueId, body}` (never `id`, which would edit a comment). `--json-schema` asks for `{ok, id, url, error}`; permission denials or `ok=false` fail the action. A second turn that may use only `get_issue` reads the issue back, and the action succeeds only when Linear reports the new state / priority / the user as assignee. A state must be one of the team's workflow states, read with one `list_issue_statuses` turn per team and cached in `claude_code_state` (platform `work_actions`, key `states:<TEAM>`) for 24 h.
- **GitHub** (ready for review, back to draft, request reviewers, comment, update branch, merge), with `gh` and no model turn: `gh pr ready N -R o/r [--undo]`, `gh pr edit --add-reviewer`, `gh pr comment --body-file -` (body on stdin), `gh pr update-branch`, `gh pr merge N -R o/r --squash|--merge|--rebase --match-head-commit <sha> [--delete-branch]`. `--admin` and `--auto` are never built. Before each command the PR is read live (one GraphQL call with `viewer`): the author must be the viewer, the PR open, and for a merge the method one the repository allows, the PR not a draft and its head the commit the user confirmed. `gh` runs in an empty temp directory with `GH_PROMPT_DISABLED=1`, so it never touches a local checkout.
- After an action the row's fresh data replaces it in the stored My work section (`my_work.replace_cached_item`, which also broadcasts `my_work_updated`); a completed / canceled issue or a merged / closed PR is dropped. The PR query also reads `headRefOid`, `mergeStateStatus`, the author and the repository's `mergeCommitAllowed` / `squashMergeAllowed` / `rebaseMergeAllowed` / `deleteBranchOnMerge`.
- UI: `lib/components/work/LinearRowActions.svelte` (state picker + menu) and `PrRowActions.svelte` (inline Ready for review / Merge + menu, merge confirm dialog with repo#number, title, method, head commit, checks, review, merge state and delete-branch checkbox); pure logic in `lib/work/actions.ts`.

## Claude terminal

`/terminal` ("Assistant" in the nav) is a plain terminal that runs the user's own coding agent CLI (`claude` by default, or Codex) (`claude_code/terminal.py`, UI `ui/src/routes/terminal/+page.svelte`).

- Spawn: only `config.get_agent_binary("claude_code")`, in a PTY (stdlib `pty.openpty`, own session and process group, the PTY as controlling terminal), with the user's environment (`TERM=xterm-256color`) and Claude Code configuration. Arguments: `--session-id <uuid>` (or `--resume <uuid>` when a closed session is resumed) and `--mcp-config <0600 temp file>`. Nothing else: no permission mode, setting sources, agents or system prompt.
- MCP: the config file points at Laya's Streamable HTTP endpoint (`/mcp/`) with a per-session turn token limited to the read tools, without expiry; it is revoked and the file deleted when the process exits or the tab closes. When Laya's MCP tools are not available the session starts without `--mcp-config` and carries a `note`.
- Assistant (`claude_code/assistant.py`): Laya's MCP server returns `instructions` in its initialize result, the text of `claude_code/assistant_instructions.md` (what Laya is, which Laya tool answers which question) followed by the working rules: settings `assistant.rules` (Settings → Agent → Assistant rules, `GET /claude-code/assistant/rules`), or the neutral `assistant.DEFAULT_RULES` while it is empty (links on every item, live re-check before calling something open, drafts only, work scope, two lanes with effort). It is re-read for every MCP session. The read tool `today` (MCP only, not in Laya's chat toolset) returns in one call the board lanes (In progress, To do, top of Backlog by priority, with link, effort and last activity), My work from its cache only (assigned Linear issues by state; own PRs grouped like the My work view, with the cache age), the cards closed today and the open items idle for more than 24 h (`recheck`). It never starts a model turn. Card tools (`search_cards`, `get_card`, `get_cards_by_entity`, `get_cards_for_event`, `get_entity`) return each card's `source_url`.
- Sessions live in the engine (`_sessions`), so tabs survive navigating away; the last 200 KB of output are replayed when a socket attaches. Rows in `claude_code_terminal_sessions` keep closed sessions for the "Resume…" menu (`resumable` = Claude Code stored a transcript for the id). On engine start rows left open are marked closed; on shutdown (`stop_service`) every session is closed.
- Socket `/claude-code/terminal/{id}/pty`: authenticated like `/ws` (app token as the second `Sec-WebSocket-Protocol` entry, origin guard). Server → client: binary output frames, text `{"type":"status",…}` and `{"type":"exit","code"}`. Client → server: binary input frames, text `{"type":"resize","cols","rows"}`.
- "Send to Assistant" (ActionCard, CardDetail, My work Linear and PR rows) sends a one-line reference through `POST /claude-code/terminal/{id}/input` to the docked assistant session (section "Assistant panel"): card `[<platform> card] <header> — <source_url>`, Linear `[<identifier>] <title> — <url>`, PR `[<repo>#<number>] <title> — <url>`.
- Agents (`claude_code/terminal_agents.py`): a session runs one coding agent CLI, chosen per tab (new-tab menu) or for the panel; the default is the configured `coding_agent` when the terminal can start it, else the first available one. `GET /terminal/sessions` lists `agents` (`available`, `reason`) and `default_agent`; `POST` takes `agent`. Claude Code: as above. Codex: `codex [resume <thread id>] --no-daemon --sandbox read-only --ask-for-approval on-request`, Laya's server added with `-c mcp_servers.laya.url=…` and `-c mcp_servers.laya.bearer_token_env_var=LAYA_TERMINAL_MCP_TOKEN` (the token travels in the environment, never in argv), and the assistant instructions passed as `-c developer_instructions=…` (codex-native; not relying on MCP server instructions). Codex picks its own thread id; it is read from codex's state database (`$CODEX_HOME/state_*.sqlite`, the earliest unclaimed thread in the session's folder created after the session started) when the session ends or is listed, and stored as `codex_cli:<thread id>` in `claude_session_id` (a bare uuid is Claude Code; no new column). Gemini CLI, Cursor and Pi are listed as "not supported in the terminal yet" and cannot be started.
- Links: printed URLs and OSC 8 hyperlinks open through the app's external-link opener (`@xterm/addon-web-links` and xterm's `linkHandler`). Keys with Ctrl/Cmd typed into a terminal stop at the terminal, so app shortcuts (Ctrl/Cmd+P/O/S/D) do not fire there; Cmd+P/O/S/D also skip the browser's dialogs (xterm does not send Cmd to the program). Right-click on a selection offers "Copy".

## Assistant panel

The assistant is docked as a right-hand side panel available on every page (`lib/components/claude/AssistantPanel.svelte`, mounted in `+layout.svelte` beside the chat sidebar, outside the route's `{#key}`, so it survives navigation). It hosts one persistent terminal session ("Assistant") of the chosen agent at 12 px.

- Slot (`lib/stores/sidePanel.ts`): `panel` = `'assistant' | 'chat' | null`, shared with Laya Chat (opening one closes the other; Laya Chat keeps `chatOpen`). Width 440–800 px (default 520, drag the left edge or use the arrow keys on it). Panel, width and the session id persist in localStorage. `--side-panel-w` on the root is the slot's width; the main area pads for it.
- Open/close: the titlebar button or `` ` `` (same guard as the other single-key shortcuts, so typing into a terminal or input never toggles it). The header shows the agent and offers "Open in Assistant view", "Laya chat" and close.
- Session: on open the panel attaches to its session (the engine replays the screen); if the engine no longer has it (restart) it resumes it (`create({resume})`), or, without a stored conversation, starts a new one and says so. After the agent exits: "Start again" with an agent choice.
- One view per session: two xterm views of one session would fight over the PTY size. The Assistant view (`/terminal`) shows the docked session as a tab marked "docked" with "Open here" (closes the panel, attaches in the view); the panel's "Open in Assistant view" does the reverse, and opening the panel takes the session back.
- "Send to Assistant" targets the docked session: it opens the panel when closed, inserts the reference once (no Enter) and focuses the terminal; while the session starts, the reference waits and is inserted when it is ready. When the docked session is shown in the Assistant view, it is inserted there.
- The nav label of `/terminal` is "Assistant" (views registry id `claude` unchanged, so stored hidden-view settings keep working).

## Board

`/board` lanes come from Laya's own data: **In progress** = status `agent_running` / `awaiting_input` / `executing`, or system tag `lane:in-progress`; **To do** = system tag `lane:todo`; **Done** = status `done` (last 7 days); **Backlog** = every other active card. Dragging changes the card status (`/cards/{id}/done`, `/reopen`, `/archive`) and the lane tags (`/tags/assign`, `/tags/unassign`). The lane tags are created as system tags on first use.

Order: in Backlog, To do and In progress the user orders cards by hand; Done is always newest first. The order is stored in `board_positions` (`card_id`, `lane`, `position`, `updated_at`), a table `api/board_api.py` creates on first use (no migration). `PUT /board/order {lane, card_ids}` stores a lane's full order after a drop (the UI writes it after the status/tag calls of a lane change). `GET /board` returns each card with `lane` and `position`; a position counts only in the lane it was set in, so a card moved by a status change has none. Cards without a position sit at the top of their lane, by priority, then latest activity. Positions of cards that leave the board (done, archived, dismissed, deleted) are dropped on the next `GET /board`.

Drag and drop: `svelte-dnd-action` (mouse/touch events; HTML5 drag and drop does not reach the page in the Tauri window on macOS). The lifted card follows the pointer, a dashed gap shows where it lands, the other cards slide aside, lanes scroll near their edges, and the Archive column is a drop zone. A press that moves less than 3px is a click. Keyboard: Tab to a card, Space picks it up, arrow keys move it, Tab moves it to another lane, Space drops it; Enter opens it. Pure logic (lanes, sort, filtered-drop merge) in `ui/src/lib/board/board.ts`.

## Effort and session lane

- Effort: the stager also returns `effort: {you_minutes, agent_minutes, basis}` (Kurt's time: deciding, reviewing, talking; agent time: code, tests, write-ups a Claude session can do). It is stored in the card's `staged_output` and shown on the card; the Board shows the sum of Kurt's time per lane.
- "Prepare session": a card action that produces a prompt in Kurt's style (what is wrong, for whom, where with links/addresses/Linear keys, what done looks like, "commit, open a PR, get it green"; merging waits for Kurt) plus a worktree command for the repository (`git -C <checkout> worktree add ../<repo>-<topic> -b <topic> origin/<default>`). Kurt copies it and starts the session himself.
- A session's claim is a lead: a card shows "merged" / "PR green" only after `gh` confirms it.

Implementation:

| Part | Where |
|---|---|
| Effort | `sessions/effort.py`; prompt block `EFFORT_RULES` + schema property in `llm/prompts/stager.py`, sent only while settings `pipeline.effort_estimate` is on (default). Stored as `staged_output.effort = {you_minutes, agent_minutes, basis, set_by}` (`set_by` = `stager` or `user`); copied to `CardResponse.effort` on every payload, the slim list included. A workspace agent's result keeps it (`staged_output_keeping_effort`). |
| Correction | `PUT /cards/{id}/effort` `{you_minutes, agent_minutes, basis?}`; broadcasts `card_updated` with `{effort}`. |
| Prepare session | `POST /cards/{id}/session-prep` → `{prompt, topic, repo, worktree_path, commands, notes, llm_error}` (`sessions/prep.py`). One LLM call on the stager's model (override key `session_prep`); without it a template prompt from the card. Repository: GitHub entity id, event `github_repo`, a github.com URL on the card, or a `repos.json` entry the card names. Checkout: the `repos.json` path, else `<root>/<repo>` under settings `sessions.checkout_roots` (Settings → Agent → Repository checkout folders; empty = the home folder), else a clone command first. After the worktree command: `cp` of untracked env files and the install command of the checkout's lock file. |
| Claims | `GET /cards/{id}/session-claims` (`sessions/verify.py`): merged / green / deployed claims in the card's text, grouped per PR, checked with `gh pr view --json state,statusCheckRollup`; each `confirmed`, `contradicted` or `unverified`. The UI (`SessionClaims.svelte`, card detail and workspace) shows only confirmed claims as facts. |

## Claude accounts

Settings → **Models → Access** (`lib/components/settings/AccessConfig.svelte`, rendered in `ModelConfig.svelte`; pure logic in `lib/utils/claudeAccounts.ts`); engine `claude_code/accounts.py`. The section lists the logins per coding agent (Claude Code, Codex); `GET /claude-code/accounts` returns every agent's logins (each with `agent_id` and `current`) and `agents` (`agent_id`, `label`, `available`); `POST /claude-code/accounts {agent}` starts a sign-in for that agent. Old links to `?tab=claude-accounts` open Models at the Access section.

- **Accounts.** An added account is a Claude Code configuration directory `~/.laya/claude-accounts/<id>/` (0700); every `claude` call for it runs with `CLAUDE_CONFIG_DIR=<dir>`. Logins and MCP OAuth tokens stay in the CLI's own storage. Email, plan and organization id (`orgId`) come from `claude auth status --json`. An added account is eligible when it is logged in, its directory exists and it is not the same Claude account (organization, else email) as an earlier one. The CLI's own login (the "default", no row, no `CLAUDE_CONFIG_DIR`) is used **only while no account has been added** (a new account whose first sign-in is still running does not count); once one exists Laya never runs on whatever the CLI is logged in to, so `/login` in a Claude Code session changes nothing in Laya. When accounts were added but none is eligible, nothing runs on Claude Code (`NoEligibleAccount`; the agent budget pauses with the accounts reason).
- **Add account.** `claude auth login --claudeai` runs in a PTY (cwd and config dir = the account directory). The CLI prints the authorize URL (OSC 8 link) and `Paste code here if prompted >`; the code is written to the PTY followed by a carriage return. Exit code 0 and `loggedIn` in `auth status` mean success; a wrong code prints `Login failed: …` and exits 1. A sign-in that ends with the organization (or email) of another added account is logged out again and refused; the same account as the CLI login is allowed. One sign-in runs at a time; it ends after 10 minutes. A new account whose sign-in fails, is cancelled or times out is deleted.
- **Sign-in state.** The engine keeps the latest sign-in (`GET /claude-code/accounts/login`, with `expires_at`). The UI renders the panel inside the account's card (or a "New account" card) from that state on every poll, keyed by the sign-in id; reloads, list refreshes and navigation never lose it, and only the pasted text stays in the page. Authorize URLs are read from the CLI output with every escape sequence removed (OSC 8 links keep their target, CSI colour codes are dropped); the first https URL wins.
- **Remove.** `claude mcp logout <server>` for each user-scope server in the directory's `.claude.json`, `claude auth logout`, then the directory (only inside `~/.laya/claude-accounts/`), the row, its rate-limit row, its usage poll, its connector matrix and its tool listing are deleted.
- **Routing, inference stages and chat.** `accounts.pick()` before each `claude` turn of `agent_llm_call` and `agent_llm_stream`. Preference order: added accounts by creation (the CLI login alone while none is added). Sticky: the current account is kept until a window reaches its threshold (agent budget `pause_at_utilization`, default 5h 90 %, 7d 80 %); after a minimum dwell of 10 minutes (at once on a rejection) the account with most headroom that is under its thresholds takes over. A preferred account is returned to after the dwell once all its windows are 10 points below their thresholds. While every account is over, the current one stays. A turn rejected by the usage limit is retried once on the next account with room (a stream only while it has not yielded anything). When routing itself fails, the first eligible added account is used, never the CLI login.
- **Routing, collectors, My work, actions, sending.** `accounts.pick_for_platform(platform, roles)`: only accounts whose connector check (stored matrix) shows the platform connected **and** whose own tool listing (`discover.account_listing`, the account's `system/init` message, cached per account for an hour) maps the platform's required roles (plus `roles`, e.g. the send tool). Among them the account inference uses now when it has room, else the one with most headroom. The turn runs with that account's environment, its own tool names (prefixes differ per account, e.g. `claude.ai Linear` vs `linear-server`) and its own deny list. `service.sweep` picks after the pause checks and before computing the window; with no eligible account it records a failed run (`no eligible account`) and the cursor stays. The account travels on `source` (`account_id`, `account_env`, `tools`, `deny_tools`, `account_platform_tools`), so the collector, the re-check and the linked documents run on it. My work (Linear), the Linear actions and Slack/Gmail sending pick the same way and are never failed over. GitHub uses `gh` and no account. Discovery for connecting a platform and the terminal use the CLI login.
- **Usage.** Every turn records its `rate_limit_info` in `agent_rate_limit_state` under `claude_code@<id>` (added account) or `claude_code@org:<orgId>` (the CLI login, keyed by the organization it is logged in to, so a `/login` starts from a clean row); legacy rows `claude_code` / `claude_code@default` are deleted. `cli.py` turns run in `stream-json` and record for the account they ran on. Windows come from `unifiedWindows` (top-level fields fill a missing one); a window whose `resetsAt` has passed counts as reset; a rejection without a signal blocks the account for 30 minutes. An added account whose organization changes loses its rate-limit row and usage poll. **Usage poll:** every 10 minutes per account (service loop) and on "Refresh", `claude auth status --json` (signed out → the account shows "Signed out", no poll), then `claude -p "/usage" --output-format json` in an empty temp dir (0 tokens). Lines `Current session: N% used · resets …` (`five_hour`) and `Current week (all models): …` (`seven_day`) are parsed; other `Current week (<model>)` lines are shown only. A missing line keeps its last value, never 0 %. Stored in `claude_code_state` (platform `accounts`, key `usage:<id>` / `usage:org:<orgId>`). The newer of poll and turn gives the utilization; status (rejection) and epoch `resetsAt` stay with the turn's signal; `/usage`'s reset words are shown. A poll is skipped while a sign-in runs in the directory, and (scheduled polls only) when a turn recorded both windows in the last 10 minutes. The audit model id stays `agent/claude_code/<model>`, the account is in the metadata (`claude_account`). The agent budget pauses for Claude Code only when every eligible account is over its threshold, until the first of them recovers.
- **Connector matrix** (read-only, stored in `claude_code_state` platform `accounts`, key `connectors:<id>`, refreshed by "Re-check" and after a sign-in): Slack, Gmail, Google Drive (claude.ai connectors; "Connect" opens `https://claude.ai/customize/connectors` for whichever account the browser is logged in to), Linear and Notion (when the default login has them as URL servers: "Add" runs `claude mcp add --scope user --transport <type> <name> <url>` in the account, copying only type and URL; "Log in" runs `claude mcp login <server> --no-browser` in a PTY; the UI opens the authorize URL once per sign-in, shows "Waiting for you to finish in the browser…", and the CLI's localhost redirect completes it (process exit 0, then a connector re-check); "Copy link" and "Paste redirect URL" remain as fallback), GitHub (`gh`, the same for every account). States: connected, needs sign-in (`needs-auth`), failed, starting (`pending`), not connected.

## Codex logins

`claude_code/codex_accounts.py`; rows of `claude_code_accounts` with `agent_id = 'codex_cli'`.

- **Logins.** An added login is a Codex home `~/.laya/codex-homes/<id>/` (0700); every `codex` call for it runs with `CODEX_HOME=<dir>`. The Codex CLI's own login (`codex-default`, no row) is used only while no Codex login is added, and only when it is logged in. Eligible: logged in, home present, not the same ChatGPT account (account id, else email) as an earlier row.
- **Sign-in.** `codex login --device-auth` in a PTY (cwd and `CODEX_HOME` = the home). The engine reads the page URL and the one-time code (`user_code` in the sign-in state) from the output; the user opens the page and enters the code, the CLI exits 0, and `account/read` must report the login. A login of a ChatGPT account already added is logged out again and refused. Cancel, failure or timeout delete a new login's row and home. Laya never completes a sign-in itself.
- **Identity and usage** come from `codex app-server` over stdio, no model call: `account/read` (email, plan, `chatgptAccountId`) and `account/rateLimits/read` (per window `usedPercent`, `windowDurationMins`, `resetsAt`). The windows are stored in `agent_rate_limit_state` as a Claude-shaped `rate_limit_info` (`unifiedWindows`: 300 min → `five_hour`, 10080 min → `seven_day`, utilization = percent / 100, `rejected` at 100 % or when the server reports the limit reached) under `codex_cli@<id>` (added login) or `codex_cli@acct:<chatgptAccountId>` (the CLI login). Polled every 10 minutes while Codex has logins or a role runs on Codex / Automatic, and on "Refresh".
- **Limit errors.** A turn whose `turn.failed` / `error` message is a usage limit records a rejection with the reset time it names ("try again at 10:14 AM" → the next 10:14 local; "at Nov 22nd, 2026 3:04 PM"; "in 2 days 3 hours"; none → 30 minutes) and raises `AgentRateLimited`, which fails over to the next Codex login with room.
- **Inference.** `codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral --ignore-user-config [--model m] [--output-schema <file>]` in an empty temp dir. The output schema is the stage schema in strict form (every property required, optional ones nullable, `additionalProperties: false`); nulls of optional properties are dropped before validation. A schema strict mode cannot express, or one Codex refuses, goes as a prompt instruction instead.

## Automatic (`agent/auto`)

The role model value `agent/auto` ("Automatic" in Settings → Models and the setup wizard's Model access step) runs each call on whichever subscription login has room. It extends upstream's agent backend (`llm/agent_backend.py`); connecting a platform without an API key (`connect.use_subscription_models`) and the wizard write it for every role.

- **Per call** `client.py` expands `agent/auto` to `agent/auto/<role>` (`agent_backend.expand_auto`). `choose_auto_agent` takes the installed agents with logins (`accounts.LOGIN_AGENTS`: Claude Code, Codex), native structured output first, then that order; an agent counts when it has a usable login (`login_module(agent).eligible_ids`; the Claude CLI login only when logged in), judged by the usage windows of the login it would run on. The choice is sticky with `accounts.choose`'s hysteresis and dwell, kept apart for stage and streaming calls. Within the agent the login is picked as before (`pick`, `failover`).
- **Models per role:** Claude Code `haiku` for router / omni / group summary, `sonnet` otherwise; Codex its default model. Audit rows carry the concrete agent model (`AgentResult.resolved_model`).
- **Failover:** a usage-limit rejection moves to the next login of the same agent; when none has room, to the next agent (stages only). Chat and Coherence (streaming, Laya's tools) run only on agents in `STREAMING_AGENTS` (Claude Code).
- **Exhausted:** when every login of every agent is over its threshold, the agent budget pauses until the earliest recovery and the call is refused like any budget-paused call (`AgentBudgetRefused`).
- **API keys** only when the user turns on "Use API keys when subscriptions are exhausted" (settings `model_access.api_fallback`, default off) and the $ budget is not paused: the first provider with a key among Anthropic, OpenAI, Google, light / heavy model per role (`API_FALLBACK_MODELS`).
- `GET /claude-code/auto` → `{roles: {<role>: {automatic, via: agent|api|none, agent_id, agent_label, login, login_label, model, reason}}, api_fallback}`: what each role runs on now; the Models tab shows it per role.

## Connector jobs on Codex

Collecting, My work (Linear) and the re-check / linked-document turns of a sweep may run on a Codex login; sending (Slack, Gmail) and My work write actions (Linear) do not.

- **Inventory** (`codex_accounts.listing`): `mcpServerStatus/list` of `codex app-server` in the login's `CODEX_HOME` (no model call), cached an hour. Tool names become `mcp__<server>__<tool>` and go through discovery's role mapping (`discover.map_tools`), so a Codex login has the same `tools` / `platform_tools` as a Claude account. Re-check in Access refreshes it and stores the connector matrix (read-only for Codex logins).
- **Gating** (`accounts.pick_for_platform(platform, roles, agents)`): candidates are the Claude Code logins, then the Codex logins; a Codex login qualifies when its inventory maps the platform's required roles. Codex's `-c` overrides cannot address a server whose name contains a dot, so a login with such a server (other than the platform's own) is not used for connector jobs and the reason says so.
- **Turn** (`cli.run_collect(..., agent_id="codex_cli")`): `codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral --disable apps` with `-c mcp_servers.<server>.enabled_tools=[…]` on the platform's server, `-c mcp_servers.<other>.enabled=false` on every other server, the schema as `--output-schema` (strict form; else as a prompt instruction), audit model `agent/codex_cli`, limit errors recorded for the login.
- **Sending and write actions stay on Claude Code** (`accounts.READ_WRITE_AGENTS`). Verified on Codex 0.166 without a model call: `enabled_tools` narrows a server's tool listing to exactly the allowed tools (Linear 78 → 2), `disabled_tools` removes single tools, `enabled=false` removes a server and `--disable apps` the ChatGPT apps server. Not verified: a real Codex model turn honouring that restriction (the Codex login was at its usage limit), so a turn that changes something keeps running only where the per-tool deny is verified end to end.

## Work scope

The GitHub source's `orgs` (empty = every repository the `gh` login can see) is the work scope collecting and My work read. The setup wizard sets it in the "Connect tools" step once GitHub is connected (`SetupWorkScope.svelte`: the organizations from `GET /claude-code/github/orgs` — `gh api user/orgs` plus the configured `selected` — as checkboxes, with a warning while none is checked), written with `PUT /claude-code/github/config` like Settings → Integrations → GitHub. My work shows the scope at the top ("Scope: a, b · Change", linking to that setting).
