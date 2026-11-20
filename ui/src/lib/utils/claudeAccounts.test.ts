import { describe, expect, it } from 'vitest';
import type { ClaudeAccountConnector, ClaudeAccountLogin } from '$lib/api/types';
import {
	accountBadge,
	accountTitle,
	countdown,
	loginPlacement,
	shouldAutoOpen,
	connectorAction,
	isLoginActive,
	loginStateText,
	planLabel,
	resetLabel,
	usageBars
} from './claudeAccounts';

const T = { five_hour: 0.9, seven_day: 0.8 };
const NOW = Date.UTC(2026, 10, 19, 12, 0, 0);
const sec = (ms: number) => Math.round(ms / 1000);

describe('usageBars', () => {
	it('shows both windows with thresholds, unknown ones as null', () => {
		const bars = usageBars(
			{ usage: { windows: { seven_day: { utilization: 0.88, resets_at: sec(NOW + 2 * 3600_000), status: 'allowed_warning' } }, as_of: null } },
			T,
			NOW
		);
		expect(bars.map((b) => b.key)).toEqual(['five_hour', 'seven_day']);
		expect(bars[0]).toMatchObject({ percent: null, threshold: 90, over: false, resets: null });
		expect(bars[1]).toMatchObject({ percent: 88, threshold: 80, over: true, resets: 'resets in 2 h' });
	});

	it('counts a rejection as over', () => {
		const bars = usageBars(
			{ usage: { windows: { five_hour: { utilization: 0.4, resets_at: null, status: 'rejected' } }, as_of: null } },
			T,
			NOW
		);
		expect(bars[0].over).toBe(true);
	});
});

describe('resetLabel', () => {
	it('formats minutes, hours and later days', () => {
		expect(resetLabel(sec(NOW + 25 * 60_000), NOW)).toBe('resets in 25 min');
		expect(resetLabel(sec(NOW - 1000), NOW)).toBe('reset');
		expect(resetLabel(null, NOW)).toBeNull();
		expect(resetLabel(sec(NOW + 3 * 86400_000), NOW)).toMatch(/^resets \S+ /);
	});
});

const conn = (o: Partial<ClaudeAccountConnector>): ClaudeAccountConnector => ({
	label: 'X', kind: 'mcp', state: 'missing', server: null, detail: null, can_add: false, ...o
});

describe('connectorAction', () => {
	it('connects claude.ai connectors on claude.ai', () => {
		expect(connectorAction(conn({ kind: 'claude_ai', state: 'needs_login' }), false)).toBe('connect');
		expect(connectorAction(conn({ kind: 'claude_ai', state: 'missing' }), true)).toBe('connect');
		expect(connectorAction(conn({ kind: 'claude_ai', state: 'connected' }), false)).toBeNull();
	});

	it('adds and signs in MCP servers only in added accounts', () => {
		expect(connectorAction(conn({ can_add: true }), false)).toBe('add');
		expect(connectorAction(conn({ state: 'needs_login', server: 'linear-server' }), false)).toBe('login');
		expect(connectorAction(conn({ can_add: true }), true)).toBeNull();
		expect(connectorAction(conn({ kind: 'gh' }), false)).toBeNull();
	});
});

describe('login helpers', () => {
	const login = (o: Partial<ClaudeAccountLogin>): ClaudeAccountLogin => ({
		id: 'f', kind: 'account', account_id: 'a', server: null, platform: null,
		state: 'waiting_code', url: 'https://claude.com/x', error: null, started_at: '', ...o
	});

	it('knows active states and describes each state', () => {
		expect(isLoginActive(login({}))).toBe(true);
		expect(isLoginActive(login({ state: 'done' }))).toBe(false);
		expect(isLoginActive(null)).toBe(false);
		expect(loginStateText(login({}))).toContain('paste the code');
		expect(loginStateText(login({ state: 'failed', error: 'Login failed: nope' }))).toBe('Login failed: nope');
		expect(loginStateText(login({ kind: 'connector', server: 'linear-server' }))).toContain('linear-server');
	});

	it('labels plans and accounts', () => {
		expect(planLabel('max')).toBe('Max');
		expect(planLabel('custom')).toBe('Custom');
		expect(planLabel(null)).toBeNull();
		expect(accountTitle({ email: null, default: true })).toBe('Your Claude Code login');
		expect(accountTitle({ email: 'a@b.c', default: false })).toBe('a@b.c');
	});
});

describe('sign-in panel placement', () => {
	const flow = (over: Partial<ClaudeAccountLogin>): ClaudeAccountLogin => ({
		id: 'f1',
		kind: 'connector',
		account_id: 'a1',
		server: 'linear-server',
		platform: 'linear',
		state: 'waiting_code',
		url: 'https://mcp.linear.app/authorize?x=1',
		error: null,
		started_at: '2026-11-20T08:00:00+00:00',
		expires_at: '2026-11-20T08:10:00+00:00',
		...over
	});
	const list = [{ id: 'default' }, { id: 'a1' }];

	it('renders in the card of the account the sign-in is for', () => {
		expect(loginPlacement(flow({}), list, new Set())).toBe('a1');
		expect(loginPlacement(null, list, new Set())).toBeNull();
	});

	it('renders a pending new account before the list shows its row', () => {
		const add = flow({ kind: 'account', account_id: 'n1', new_account: true, state: 'starting' });
		expect(loginPlacement(add, list, new Set())).toBe('new');
		expect(loginPlacement({ ...add, state: 'failed' }, list, new Set())).toBeNull();
	});

	it('keeps an ended sign-in until dismissed, and hides a finished one', () => {
		const failed = flow({ state: 'failed', error: 'nope' });
		expect(loginPlacement(failed, list, new Set())).toBe('a1');
		expect(loginPlacement(failed, list, new Set(['f1']))).toBeNull();
		expect(loginPlacement(flow({ state: 'done' }), list, new Set())).toBeNull();
		// Dismissing never hides a running sign-in.
		expect(loginPlacement(flow({}), list, new Set(['f1']))).toBe('a1');
	});

	it('opens a connector page once per sign-in, never an account sign-in', () => {
		expect(shouldAutoOpen(flow({}), new Set())).toBe(true);
		expect(shouldAutoOpen(flow({}), new Set(['f1']))).toBe(false);
		expect(shouldAutoOpen(flow({ url: null }), new Set())).toBe(false);
		expect(shouldAutoOpen(flow({ state: 'starting' }), new Set())).toBe(false);
		expect(shouldAutoOpen(flow({ kind: 'account' }), new Set())).toBe(false);
	});

	it('counts down to the expiry', () => {
		const start = Date.parse('2026-11-20T08:00:00+00:00');
		expect(countdown('2026-11-20T08:10:00+00:00', start)).toBe('10:00 left');
		expect(countdown('2026-11-20T08:10:00+00:00', start + 9 * 60_000 + 55_000)).toBe('0:05 left');
		expect(countdown('2026-11-20T08:10:00+00:00', start + 11 * 60_000)).toBeNull();
		expect(countdown(undefined, start)).toBeNull();
	});

	it('tells the user to finish a connector sign-in in the browser', () => {
		expect(loginStateText(flow({}))).toContain('Waiting for you to finish');
	});
});

describe('account badges and reset texts', () => {
	const acc = { id: 'a1', default: false, logged_in: true, in_use: true, pending: false };

	it('marks the account processing now, signed-out and unused ones', () => {
		expect(accountBadge(acc, 'a1')).toEqual({ label: 'Processing now', tone: 'active' });
		expect(accountBadge({ ...acc, logged_in: false }, 'a1')).toEqual({ label: 'Signed out', tone: 'warn' });
		expect(accountBadge({ ...acc, id: 'default', default: true, in_use: false }, 'a1')).toEqual({
			label: 'Not used',
			tone: 'muted'
		});
		expect(accountBadge({ ...acc, pending: true, logged_in: false }, null)?.label).toBe('Signing in');
		expect(accountBadge(acc, 'a2')).toBeNull();
	});

	it('uses the /usage reset text when the rate-limit signal has no epoch', () => {
		const bars = usageBars(
			{
				usage: {
					windows: {
						seven_day: { utilization: 1, resets_at: null, status: null, resets_text: 'Nov 25 at 4am' }
					},
					as_of: null
				}
			},
			{ seven_day: 0.8 }
		);
		expect(bars[1]).toMatchObject({ percent: 100, over: true, rejected: false, resets: 'resets Nov 25 at 4am' });
	});
});
