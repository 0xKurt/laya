// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure helpers of the Claude Code connector UI (docs/claude-code-connector.md):
 * the status → list entry mapping, the config form ↔ config object mapping,
 * and the identity → team member mapping the setup wizard uses.
 */

import type {
	ClaudeCodeConfig,
	ClaudeCodeIdentity,
	ClaudeCodePlatform,
	ClaudeCodePlatformStatus,
	EgressConnection
} from '$lib/api/types';

export const CLAUDE_CODE_PLATFORMS: ClaudeCodePlatform[] = ['slack', 'gmail', 'linear', 'github', 'notion'];

export const CLAUDE_CODE_LABELS: Record<ClaudeCodePlatform, string> = {
	slack: 'Slack',
	gmail: 'Gmail',
	linear: 'Linear',
	github: 'GitHub',
	notion: 'Notion'
};

export const CLAUDE_CODE_PREFIX = 'claude_code:';

export function isClaudeCodePlatform(key: string): key is ClaudeCodePlatform {
	return (CLAUDE_CODE_PLATFORMS as string[]).includes(key);
}

export function isClaudeCodeConnection(c: { connection_id: string }): boolean {
	return c.connection_id.startsWith(CLAUDE_CODE_PREFIX);
}

/** Rolled-up state of one platform's Claude Code source. */
export type ClaudeCodeState = 'off' | 'ok' | 'paused' | 'error' | 'unavailable';

export function claudeCodeState(s: ClaudeCodePlatformStatus | null | undefined): ClaudeCodeState {
	if (!s || !s.connected) return 'off';
	// A connector that went missing in Claude Code breaks every sweep.
	if (!s.available) return 'unavailable';
	if (s.last_error) return 'error';
	if (!s.enabled) return 'paused';
	return 'ok';
}

/** The user-facing reason a platform cannot be used, or null. */
export function claudeCodeProblem(
	s: ClaudeCodePlatformStatus | null | undefined,
	cliFound: boolean
): string | null {
	if (!cliFound) return 'Claude Code is not installed or not on the PATH. Install it and log in by running `claude` once.';
	if (!s) return null;
	if (s.available) return null;
	return s.problem || 'Not available in your Claude Code session.';
}

/**
 * The Claude Code source as an account of its platform, so the Integrations
 * list counts it and shows its health like any n8n account. Null when the
 * platform is not connected through Claude Code.
 */
export function claudeCodeConnection(
	platform: ClaudeCodePlatform,
	s: ClaudeCodePlatformStatus | null | undefined
): EgressConnection | null {
	const state = claudeCodeState(s);
	if (!s || state === 'off') return null;
	const failing = state === 'error' || state === 'unavailable';
	return {
		connection_id: `${CLAUDE_CODE_PREFIX}${platform}`,
		platform,
		name: state === 'paused' ? 'via Claude Code · paused' : 'via Claude Code',
		status: failing ? 'error' : 'connected',
		capabilities: [],
		error_message: failing ? (s.last_error ?? s.problem ?? undefined) : undefined,
		last_validated_at: s.last_ok_at ?? undefined,
		created_at: ''
	};
}

/**
 * A platform's accounts with the Claude Code source in place of any egress
 * row the connector created for sending (one entry per platform, not two).
 */
export function withClaudeCodeConnections(
	connections: EgressConnection[],
	platforms: Partial<Record<ClaudeCodePlatform, ClaudeCodePlatformStatus>> | null
): EgressConnection[] {
	const kept = connections.filter((c) => !isClaudeCodeConnection(c));
	if (!platforms) return kept;
	const added = CLAUDE_CODE_PLATFORMS.map((p) => claudeCodeConnection(p, platforms[p])).filter(
		(c): c is EgressConnection => c !== null
	);
	return [...kept, ...added];
}

/** "Kurt Example · kurt@example.com" from whatever the platform returned. */
export function identityLabel(identity: ClaudeCodeIdentity | null | undefined): string {
	if (!identity) return '';
	const handle =
		identity.email || identity.github_login || identity.slack_user_id || identity.linear_user_id || identity.notion_user_id || '';
	const name = identity.name || '';
	if (name && handle && name !== handle) return `${name} · ${handle}`;
	return name || handle;
}

/** "3 events" / "1 event" / "no new events". */
export function eventsLabel(n: number | null | undefined): string {
	if (!n) return 'no new events';
	return n === 1 ? '1 event' : `${n} events`;
}

// ── config form ──────────────────────────────────────────────────────────

/** The editable form of a platform's config: every field, lists as text. */
export interface ClaudeCodeForm {
	interval_minutes: number;
	start: string;
	end: string;
	days: number[];
	timezone: string;
	model: string;
	initial_lookback_hours: number;
	recheck_after_hours: number;
	context_messages: number;
	thread_memory_days: number;
	only_relevant: boolean;
	/** One address per line. */
	exclude_senders: string;
	meeting_notes: boolean;
	/** Comma or space separated. */
	orgs: string;
	others_prs_only_on_mention: boolean;
}

/** Config keys each platform has beyond the common ones. */
export const PLATFORM_KEYS: Record<ClaudeCodePlatform, (keyof ClaudeCodeConfig)[]> = {
	slack: ['context_messages', 'thread_memory_days', 'only_relevant'],
	gmail: ['exclude_senders', 'meeting_notes'],
	github: ['orgs', 'others_prs_only_on_mention'],
	linear: [],
	notion: []
};

export const MODELS = [
	{ value: 'haiku', label: 'Haiku (fast, light on your usage)' },
	{ value: 'sonnet', label: 'Sonnet' },
	{ value: 'opus', label: 'Opus' }
];

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export function configToForm(c: ClaudeCodeConfig): ClaudeCodeForm {
	return {
		interval_minutes: c.interval_minutes,
		start: c.active_hours?.start ?? '08:00',
		end: c.active_hours?.end ?? '19:00',
		days: [...(c.active_hours?.days ?? [0, 1, 2, 3, 4])].sort((a, b) => a - b),
		timezone: c.timezone ?? '',
		model: c.model || 'haiku',
		initial_lookback_hours: c.initial_lookback_hours,
		recheck_after_hours: c.recheck_after_hours,
		context_messages: c.context_messages ?? 8,
		thread_memory_days: c.thread_memory_days ?? 14,
		only_relevant: c.only_relevant ?? true,
		exclude_senders: (c.exclude_senders ?? []).join('\n'),
		meeting_notes: c.meeting_notes ?? true,
		orgs: (c.orgs ?? []).join(', '),
		others_prs_only_on_mention: c.others_prs_only_on_mention ?? true
	};
}

function splitList(text: string, lower: boolean): string[] {
	const seen = new Set<string>();
	const out: string[] = [];
	for (const raw of text.split(/[\s,;]+/)) {
		const v = lower ? raw.trim().toLowerCase() : raw.trim();
		if (v && !seen.has(v.toLowerCase())) {
			seen.add(v.toLowerCase());
			out.push(v);
		}
	}
	return out;
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

function wholeNumber(v: unknown): number | null {
	const n = typeof v === 'number' ? v : Number(String(v ?? '').trim());
	return Number.isFinite(n) && n >= 0 && Number.isInteger(n) ? n : null;
}

/**
 * The full config object PUT /claude-code/{platform}/config takes, or the
 * first problem with the form in plain words.
 */
export function formToConfig(
	platform: ClaudeCodePlatform,
	f: ClaudeCodeForm
): { config: ClaudeCodeConfig; error: null } | { config: null; error: string } {
	const fail = (error: string) => ({ config: null, error }) as const;
	const numbers: [keyof ClaudeCodeForm, string, number][] = [
		['interval_minutes', 'Check every', 1],
		['initial_lookback_hours', 'First look-back', 0],
		['recheck_after_hours', 'Re-check after', 0]
	];
	if (platform === 'slack') {
		numbers.push(['context_messages', 'Earlier messages', 0], ['thread_memory_days', 'Thread memory', 0]);
	}
	const parsed: Record<string, number> = {};
	for (const [key, label, min] of numbers) {
		const n = wholeNumber(f[key]);
		if (n === null || n < min) return fail(`${label} must be a whole number${min ? ` of at least ${min}` : ''}.`);
		parsed[key] = n;
	}
	if (!HHMM.test(f.start) || !HHMM.test(f.end)) return fail('Active hours must be times like 08:00.');
	if (f.days.length === 0) return fail('Pick at least one day.');

	const config: ClaudeCodeConfig = {
		interval_minutes: parsed.interval_minutes,
		active_hours: { start: f.start, end: f.end, days: [...new Set(f.days)].sort((a, b) => a - b) },
		timezone: f.timezone.trim(),
		model: f.model,
		initial_lookback_hours: parsed.initial_lookback_hours,
		recheck_after_hours: parsed.recheck_after_hours
	};
	if (platform === 'slack') {
		config.context_messages = parsed.context_messages;
		config.thread_memory_days = parsed.thread_memory_days;
		config.only_relevant = f.only_relevant;
	} else if (platform === 'gmail') {
		config.exclude_senders = splitList(f.exclude_senders, true);
		config.meeting_notes = f.meeting_notes;
	} else if (platform === 'github') {
		config.orgs = splitList(f.orgs, false);
		config.others_prs_only_on_mention = f.others_prs_only_on_mention;
	}
	return { config, error: null };
}

// ── setup wizard: identity → the "me" team member ────────────────────────

export interface SelfMember {
	name: string;
	email: string;
	accounts: string[];
	aliases: string[];
}

/**
 * The user's team member from the identities discovery read. The name and
 * primary email prefer Gmail, then Slack; Slack user id and GitHub login
 * become accounts, every other email an alias (as connect.py writes them).
 */
export function selfMemberFromIdentities(
	identities: Partial<Record<ClaudeCodePlatform, ClaudeCodeIdentity>>
): SelfMember | null {
	const order: ClaudeCodePlatform[] = ['gmail', 'slack', 'github', 'linear', 'notion'];
	const ids = order.map((p) => identities[p]).filter((i): i is ClaudeCodeIdentity => !!i);
	if (ids.length === 0) return null;
	const name = ids.map((i) => i.name?.trim()).find((n) => !!n) ?? '';
	const emails: string[] = [];
	for (const i of ids) {
		const e = i.email?.trim().toLowerCase();
		if (e && !emails.includes(e)) emails.push(e);
	}
	const accounts = [identities.slack?.slack_user_id, identities.github?.github_login].filter(
		(a): a is string => !!a
	);
	if (!name && emails.length === 0 && accounts.length === 0) return null;
	return { name, email: emails[0] ?? '', accounts, aliases: emails.slice(1) };
}

/**
 * Stage models that run on the user's Claude subscription through Claude Code.
 * Only the roles ModelConfig lets an agent serve (AGENT_ROLES): the engine's
 * streaming path (chat, Coherence/trace) rejects agent models, so those keep
 * their provider model.
 */
export const CLAUDE_SUBSCRIPTION_MODELS = {
	router: 'agent/claude_code/haiku',
	omni: 'agent/claude_code/haiku',
	stager: 'agent/claude_code/sonnet'
} as const;
