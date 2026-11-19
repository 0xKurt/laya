// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import type { ClaudeCodeConfig, ClaudeCodePlatformStatus, EgressConnection } from '$lib/api/types';
import {
	claudeCodeConnection,
	claudeCodeProblem,
	claudeCodeState,
	configToForm,
	eventsLabel,
	formToConfig,
	identityLabel,
	selfMemberFromIdentities,
	withClaudeCodeConnections
} from './claudeCode';
import { platformHealth, platformSummary } from './integrationHealth';

const COMMON: ClaudeCodeConfig = {
	interval_minutes: 60,
	active_hours: { start: '08:00', end: '19:00', days: [0, 1, 2, 3, 4] },
	timezone: '',
	model: 'haiku',
	initial_lookback_hours: 12,
	recheck_after_hours: 6
};

function status(over: Partial<ClaudeCodePlatformStatus> = {}): ClaudeCodePlatformStatus {
	return {
		available: true,
		connected: true,
		enabled: true,
		identity: {},
		problem: null,
		last_run_at: null,
		last_ok_at: null,
		last_error: null,
		last_count: null,
		config: COMMON,
		can_send: false,
		...over
	};
}

describe('claudeCodeState', () => {
	it('maps the status to one state, failures first', () => {
		expect(claudeCodeState(null)).toBe('off');
		expect(claudeCodeState(status({ connected: false }))).toBe('off');
		expect(claudeCodeState(status())).toBe('ok');
		expect(claudeCodeState(status({ enabled: false }))).toBe('paused');
		expect(claudeCodeState(status({ enabled: false, last_error: 'boom' }))).toBe('error');
		expect(claudeCodeState(status({ available: false, last_error: 'boom' }))).toBe('unavailable');
	});
});

describe('claudeCodeProblem', () => {
	it('explains a missing CLI before anything else', () => {
		expect(claudeCodeProblem(status(), false)).toMatch(/not installed/);
	});
	it("passes the engine's reason through, or nothing when available", () => {
		expect(claudeCodeProblem(status(), true)).toBeNull();
		expect(claudeCodeProblem(status({ available: false, problem: 'Run `gh auth login`.' }), true)).toBe(
			'Run `gh auth login`.'
		);
	});
});

describe('claudeCodeConnection', () => {
	it('is absent when not connected', () => {
		expect(claudeCodeConnection('slack', status({ connected: false }))).toBeNull();
	});
	it('reads as an account in the list', () => {
		const c = claudeCodeConnection('slack', status({ last_ok_at: '2026-11-19T10:00:00+00:00' }))!;
		expect(c.connection_id).toBe('claude_code:slack');
		expect(c.status).toBe('connected');
		expect(c.last_validated_at).toBe('2026-11-19T10:00:00+00:00');
		expect(platformSummary([c])).toBe('via Claude Code');
	});
	it('shows a failing sweep as an error with its message', () => {
		const c = claudeCodeConnection('gmail', status({ last_error: 'timeout' }))!;
		expect(c.status).toBe('error');
		expect(c.error_message).toBe('timeout');
		expect(platformSummary([c])).toBe('via Claude Code · Error');
	});
	it('marks a paused source without calling it failing', () => {
		const c = claudeCodeConnection('linear', status({ enabled: false }))!;
		expect(c.status).toBe('connected');
		expect(c.name).toBe('via Claude Code · paused');
	});
});

describe('withClaudeCodeConnections', () => {
	const n8n: EgressConnection = {
		connection_id: 'conn_1',
		platform: 'slack',
		name: 'Work',
		status: 'connected',
		capabilities: [],
		created_at: ''
	};
	const sendRow: EgressConnection = { ...n8n, connection_id: 'claude_code:slack', name: 'Slack (Claude Code)' };

	it('adds one entry per connected platform and drops the send row', () => {
		const merged = withClaudeCodeConnections([n8n, sendRow], {
			slack: status(),
			gmail: status({ connected: false })
		});
		expect(merged.map((c) => c.connection_id)).toEqual(['conn_1', 'claude_code:slack']);
		expect(platformHealth(merged.filter((c) => c.platform === 'slack')).n).toBe(2);
	});
	it('keeps the n8n list as is without a status', () => {
		expect(withClaudeCodeConnections([n8n], null)).toEqual([n8n]);
	});
});

describe('labels', () => {
	it('formats identity and event counts', () => {
		expect(identityLabel({ name: 'Kurt', email: 'k@x.io' })).toBe('Kurt · k@x.io');
		expect(identityLabel({ github_login: 'kurt' })).toBe('kurt');
		expect(identityLabel(undefined)).toBe('');
		expect(eventsLabel(0)).toBe('no new events');
		expect(eventsLabel(1)).toBe('1 event');
		expect(eventsLabel(4)).toBe('4 events');
	});
});

describe('config form', () => {
	it('round-trips every platform config', () => {
		const slack = { ...COMMON, context_messages: 8, thread_memory_days: 14, only_relevant: true };
		const gmail = { ...COMMON, exclude_senders: ['a@x.io', 'b@y.io'], meeting_notes: false };
		const github = { ...COMMON, orgs: ['acme', 'Other-Org'], others_prs_only_on_mention: true };
		expect(formToConfig('slack', configToForm(slack)).config).toEqual(slack);
		expect(formToConfig('gmail', configToForm(gmail)).config).toEqual(gmail);
		expect(formToConfig('github', configToForm(github)).config).toEqual(github);
		expect(formToConfig('linear', configToForm(COMMON)).config).toEqual(COMMON);
	});

	it('sends only the platform keys the engine accepts', () => {
		const form = configToForm({ ...COMMON, context_messages: 3 });
		expect(Object.keys(formToConfig('notion', form).config!)).toEqual(Object.keys(COMMON));
	});

	it('cleans list text', () => {
		const form = { ...configToForm(COMMON), exclude_senders: ' A@x.io\n\na@x.io, b@y.io ', orgs: 'acme,  beta acme' };
		expect(formToConfig('gmail', form).config!.exclude_senders).toEqual(['a@x.io', 'b@y.io']);
		expect(formToConfig('github', form).config!.orgs).toEqual(['acme', 'beta']);
	});

	it('parses numbers typed as text and sorts days', () => {
		const form = { ...configToForm(COMMON), interval_minutes: '15' as unknown as number, days: [4, 0, 2] };
		const { config } = formToConfig('linear', form);
		expect(config!.interval_minutes).toBe(15);
		expect(config!.active_hours.days).toEqual([0, 2, 4]);
	});

	it('names the first problem', () => {
		const base = configToForm(COMMON);
		expect(formToConfig('linear', { ...base, interval_minutes: 0 }).error).toMatch(/Check every/);
		expect(formToConfig('linear', { ...base, recheck_after_hours: 1.5 }).error).toMatch(/Re-check/);
		expect(formToConfig('linear', { ...base, start: '8:00' }).error).toMatch(/08:00/);
		expect(formToConfig('linear', { ...base, days: [] }).error).toMatch(/one day/);
	});
});

describe('selfMemberFromIdentities', () => {
	it('builds the member the way connect writes team.json', () => {
		expect(
			selfMemberFromIdentities({
				slack: { slack_user_id: 'U1', name: 'Kurt S', email: 'kurt@work.io' },
				gmail: { email: 'Kurt@Mail.io', name: '' },
				github: { github_login: 'kurt-gh', name: 'Kurt G', email: '' }
			})
		).toEqual({ name: 'Kurt S', email: 'kurt@mail.io', accounts: ['U1', 'kurt-gh'], aliases: ['kurt@work.io'] });
	});
	it('is null without identities', () => {
		expect(selfMemberFromIdentities({})).toBeNull();
	});
});
