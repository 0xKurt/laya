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
	rejected: boolean;
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
		const rejected = w?.status === 'rejected';
		const over = rejected || (percent !== null && percent >= threshold);
		// The epoch from the rate-limit signal is exact; /usage's own words are shown when it is missing.
		const resets = resetLabel(w?.resets_at, now) ?? (w?.resets_text ? `resets ${w.resets_text}` : null);
		return { key, label, percent, threshold, over, rejected, resets };
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
				? `Waiting for you to finish signing in to ${what} in the browser…`
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
	return a.email ?? (a.default ? 'Your Claude Code login' : 'New account');
}

/**
 * Where the sign-in panel belongs: the id of the account card it renders in,
 * `'new'` for a pending "New account" card (a new account whose row the list
 * does not show yet), or null when there is nothing to show (no sign-in, or
 * an ended one the user dismissed). Decided from the engine's sign-in state
 * alone, so a reload or a list refresh never loses the panel.
 */
export function loginPlacement(
	login: ClaudeAccountLogin | null | undefined,
	accounts: Pick<ClaudeAccount, 'id'>[],
	dismissed: ReadonlySet<string>
): string | null {
	if (!login) return null;
	if (!isLoginActive(login) && (dismissed.has(login.id) || login.state === 'done')) return null;
	if (accounts.some((a) => a.id === login.account_id)) return login.account_id;
	if (login.kind === 'account' && login.new_account && isLoginActive(login)) return 'new';
	return null;
}

/**
 * Connector sign-ins whose page was opened already, by sign-in id. Module
 * state, so returning to Settings does not open the same page again.
 */
export const openedLoginIds = new Set<string>();

/**
 * Whether a connector sign-in's page should be opened now: once per sign-in
 * (keyed by its id), as soon as the engine has the URL and is waiting.
 * Account sign-ins are opened by the user, who may want a private window.
 */
export function shouldAutoOpen(login: ClaudeAccountLogin | null | undefined, opened: ReadonlySet<string>): boolean {
	return (
		!!login &&
		login.kind === 'connector' &&
		login.state === 'waiting_code' &&
		!!login.url &&
		!opened.has(login.id)
	);
}

/** "9:41 left" until the engine stops waiting; null without an expiry or once it has passed. */
export function countdown(expiresAt: string | null | undefined, now: number = Date.now()): string | null {
	if (!expiresAt) return null;
	const end = Date.parse(expiresAt);
	if (Number.isNaN(end)) return null;
	const seconds = Math.floor((end - now) / 1000);
	if (seconds <= 0) return null;
	const m = Math.floor(seconds / 60);
	const s = seconds % 60;
	return `${m}:${String(s).padStart(2, '0')} left`;
}

/** The status badge of an account card. */
export function accountBadge(
	a: Pick<ClaudeAccount, 'id' | 'default' | 'logged_in' | 'in_use' | 'pending'>,
	current: string | null
): { label: string; tone: 'active' | 'muted' | 'warn' } | null {
	if (a.pending) return { label: 'Signing in', tone: 'muted' };
	if (!a.logged_in) return { label: 'Signed out', tone: 'warn' };
	if (current === a.id) return { label: 'Processing now', tone: 'active' };
	if (a.default && !a.in_use) return { label: 'Not used', tone: 'muted' };
	return null;
}
