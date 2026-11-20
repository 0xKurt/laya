/**
 * The work scope: the GitHub organizations the GitHub source (and My work) read,
 * i.e. the source's config `orgs` (empty = every repository the login can see),
 * the Gmail senders the Gmail source skips, and the folder check behind the
 * checkout folder list.
 */
import { engineFetch } from './engine';

export interface GithubOrgs {
	orgs: string[];
	selected: string[];
	connected: boolean;
	error: string | null;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
	const resp = await engineFetch(path, { signal: AbortSignal.timeout(120_000), ...init });
	if (!resp.ok) {
		let detail = '';
		try {
			const body = await resp.json();
			detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
		} catch {
			// no parseable body
		}
		throw new Error(detail || `Engine API error: ${resp.status}`);
	}
	return resp.json() as Promise<T>;
}

export function getGithubOrgs(): Promise<GithubOrgs> {
	return json<GithubOrgs>('/claude-code/github/orgs');
}

/** Writes `orgs` into the GitHub source's config, keeping its other options. */
export async function setGithubOrgs(orgs: string[]): Promise<void> {
	const status = await json<{ platforms: Record<string, { config?: Record<string, unknown> }> }>('/claude-code/status');
	const config = { ...(status.platforms.github?.config ?? {}), orgs };
	await json('/claude-code/github/config', { method: 'PUT', body: JSON.stringify(config) });
}

/** "acme, globex" or "all repositories" for the scope line. */
export function scopeLabel(orgs: string[] | null | undefined): string {
	return orgs && orgs.length ? orgs.join(', ') : 'all repositories';
}

// ── Gmail senders (the "Skip mail from" chips and "Skip this sender") ─────

export interface GmailSender {
	email: string;
	name: string;
	count: number;
	last_seen: string;
}

export interface GmailSenders {
	senders: GmailSender[];
	skipped: string[];
	connected: boolean;
	days: number;
}

/** Senders of recent Gmail events not skipped yet, most frequent first. */
export function getGmailSenders(days = 30, limit = 20): Promise<GmailSenders> {
	return json<GmailSenders>(`/claude-code/gmail/senders?days=${days}&limit=${limit}`);
}

async function gmailConfig(): Promise<{ connected: boolean; config: Record<string, unknown> }> {
	const status = await json<{ platforms: Record<string, { connected?: boolean; config?: Record<string, unknown> }> }>(
		'/claude-code/status'
	);
	const gmail = status.platforms.gmail;
	return { connected: !!gmail?.connected, config: { ...(gmail?.config ?? {}) } };
}

/**
 * Adds (skip = true) or removes a sender from the Gmail source's
 * `exclude_senders`, keeping its other options: the same config the settings
 * chips edit. Returns the new list.
 */
export async function setGmailSenderSkipped(email: string, skip: boolean): Promise<string[]> {
	const { connected, config } = await gmailConfig();
	if (!connected) throw new Error('Gmail is not connected through your coding agent.');
	const value = email.trim().toLowerCase();
	const current = ((config.exclude_senders as string[] | undefined) ?? []).map((s) => s.toLowerCase());
	const next = skip ? (current.includes(value) ? current : [...current, value]) : current.filter((s) => s !== value);
	await json('/claude-code/gmail/config', { method: 'PUT', body: JSON.stringify({ ...config, exclude_senders: next }) });
	return next;
}

/** Whether the Gmail source is connected through the coding agent and its skipped senders. */
export async function getGmailSkipped(): Promise<{ connected: boolean; skipped: string[] }> {
	const { connected, config } = await gmailConfig();
	return { connected, skipped: ((config.exclude_senders as string[] | undefined) ?? []).map((s) => s.toLowerCase()) };
}

// ── folders ───────────────────────────────────────────────────────────────

export interface FolderCheck {
	path: string;
	exists: boolean;
	error: string | null;
}

/** Whether a typed folder exists (browser build; the app uses the folder dialog). */
export function checkFolder(path: string): Promise<FolderCheck> {
	return json<FolderCheck>(`/claude-code/folders/check?path=${encodeURIComponent(path)}`);
}

// ── GitHub reviewers ──────────────────────────────────────────────────────

export interface GithubReviewers {
	users: string[];
	teams: string[];
	error: string | null;
}

/** Assignable users and teams of a repository, as reviewer suggestions. */
export function getGithubReviewers(repo: string): Promise<GithubReviewers> {
	return json<GithubReviewers>(`/claude-code/my-work/github/reviewers?repo=${encodeURIComponent(repo)}`);
}
