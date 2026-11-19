import { describe, expect, it } from 'vitest';
import type { ClaudeAccountConnector, ClaudeAccountLogin } from '$lib/api/types';
import {
	accountTitle,
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
