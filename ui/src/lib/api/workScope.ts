/**
 * The work scope: the GitHub organizations the GitHub source (and My work) read,
 * i.e. the source's config `orgs` (empty = every repository the login can see).
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
