/**
 * Pure helpers for Settings → Claude accounts (`ClaudeAccountsConfig.svelte`).
 */
import type {
	ClaudeAccount,
	ClaudeAccountConnector,
	ClaudeAccountLogin,
	ClaudeConnectorKey
} from '$lib/api/types';

export const WINDOWS: { key: string; label: string }[] = [
	{ key: 'five_hour', label: '5-hour window' },
	{ key: 'seven_day', label: '7-day window' }
];

export const CONNECTOR_ORDER: ClaudeConnectorKey[] = ['slack', 'gmail', 'drive', 'linear', 'notion', 'github'];

export interface UsageBar {
	key: string;
	label: string;
	/** 0..100, null when the CLI has not reported this window yet. */
	percent: number | null;
	/** The threshold at which Laya moves processing to another account, 0..100. */
	threshold: number;
	over: boolean;
	resets: string | null;
}

const PLANS: Record<string, string> = { max: 'Max', pro: 'Pro', team: 'Team', enterprise: 'Enterprise', free: 'Free' };

export function planLabel(plan: string | null | undefined): string | null {
	if (!plan) return null;
	return PLANS[plan] ?? plan.charAt(0).toUpperCase() + plan.slice(1);
}

/** "resets in 40 min" / "resets in 3 h" / "resets Mon 14:00" for a reset time in unix seconds. */
export function resetLabel(resetsAt: number | null | undefined, now: number = Date.now()): string | null {
	if (!resetsAt) return null;
	const ms = resetsAt * 1000 - now;
	if (ms <= 0) return 'reset';
	const minutes = Math.round(ms / 60_000);
	if (minutes < 60) return `resets in ${Math.max(1, minutes)} min`;
	const hours = Math.round(minutes / 60);
	if (hours < 24) return `resets in ${hours} h`;
	const d = new Date(resetsAt * 1000);
	const day = d.toLocaleDateString(undefined, { weekday: 'short' });
	const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
	return `resets ${day} ${time}`;
}

export function usageBars(
	account: Pick<ClaudeAccount, 'usage'>,
	thresholds: Record<string, number>,
	now: number = Date.now()
): UsageBar[] {
	return WINDOWS.map(({ key, label }) => {
		const w = account.usage.windows[key];
		const threshold = Math.round((thresholds[key] ?? 0.9) * 100);
		const util = w?.utilization;
		const percent = util === null || util === undefined ? null : Math.round(Math.min(1, Math.max(0, util)) * 100);
		const over = w?.status === 'rejected' || (percent !== null && percent >= threshold);
		return { key, label, percent, threshold, over, resets: resetLabel(w?.resets_at, now) };
	});
}

export type ConnectorAction = 'connect' | 'add' | 'login' | null;

/**
 * What the user can do about a connector of an account: claude.ai connectors
 * are connected on claude.ai ("connect"); Linear / Notion servers can be added
 * to an added account and signed in there. The default account and GitHub are
 * managed outside Laya.
 */
export function connectorAction(c: ClaudeAccountConnector, isDefault: boolean): ConnectorAction {
	if (c.state === 'connected' || c.kind === 'gh') return null;
	if (c.kind === 'claude_ai') return 'connect';
	if (isDefault) return null;
	if (c.can_add) return 'add';
	if (c.state === 'needs_login' || c.state === 'failed') return 'login';
	return null;
}

export const CONNECTOR_STATE_LABEL: Record<ClaudeAccountConnector['state'], string> = {
	connected: 'Connected',
	needs_login: 'Needs sign-in',
	failed: 'Failed',
	pending: 'Starting',
	disabled: 'Disabled',
	missing: 'Not connected'
};

export function isLoginActive(login: ClaudeAccountLogin | null | undefined): boolean {
	return !!login && ['starting', 'waiting_code', 'verifying'].includes(login.state);
}

export function loginStateText(login: ClaudeAccountLogin): string {
	const what = login.kind === 'connector' ? `${login.server ?? 'the connector'}` : 'the account';
	switch (login.state) {
		case 'starting':
			return 'Starting the sign-in…';
		case 'waiting_code':
			return login.kind === 'connector'
				? `Sign in to ${what} in your browser, then paste the address of the page you land on.`
				: 'Sign in on the Claude page in your browser with the account you want to add, then paste the code it shows.';
		case 'verifying':
			return 'Checking the code…';
		case 'done':
			return login.kind === 'connector' ? `Signed in to ${what}.` : 'Account added.';
		case 'cancelled':
			return 'Sign-in cancelled.';
		case 'timed_out':
		case 'failed':
			return login.error ?? 'The sign-in failed.';
	}
}

export function accountTitle(a: Pick<ClaudeAccount, 'email' | 'default'>): string {
	return a.email ?? (a.default ? 'Your Claude Code login' : 'Signing in…');
}
