/**
 * Engine calls for "Automatic" model routing (GET /claude-code/auto) and its
 * settings (`model_access`). Kept apart from engine.ts so the fork's additions
 * live in their own file.
 */
import { engineFetch } from './engine';

export interface AutoRole {
	/** Whether the role's model is "Automatic" (agent/auto). */
	automatic: boolean;
	/** agent: a coding agent login; api: the API fallback; none: nothing can run it now. */
	via?: 'agent' | 'api' | 'none';
	agent_id?: string;
	agent_label?: string;
	login?: string | null;
	login_label?: string | null;
	model?: string | null;
	reason?: string | null;
}

export interface AutoRouting {
	roles: Record<string, AutoRole>;
	api_fallback: boolean;
}

export const AUTO_MODEL = 'agent/auto';

async function json<T>(path: string, init?: RequestInit): Promise<T> {
	const resp = await engineFetch(path, init);
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

export function getAutoRouting(): Promise<AutoRouting> {
	return json<AutoRouting>('/claude-code/auto', { signal: AbortSignal.timeout(60_000) });
}

/** Turns "Use API keys when subscriptions are exhausted" on or off. */
export function setApiFallback(enabled: boolean): Promise<unknown> {
	return json('/settings', { method: 'PUT', body: JSON.stringify({ model_access: { api_fallback: enabled } }) });
}

/** "now on Claude Code · me@example.com · haiku" for a role on Automatic. */
export function describeAutoRole(r: AutoRole | undefined): string {
	if (!r) return 'Automatic';
	if (r.via === 'agent') {
		const model = r.model?.split('/').slice(2).join('/') || 'default model';
		const who = r.login_label ? ` · ${r.login_label}` : '';
		return `Automatic · now on ${r.agent_label ?? r.agent_id}${who} · ${model}`;
	}
	if (r.via === 'api') return `Automatic · subscriptions exhausted, on API model ${r.model}`;
	return `Automatic · ${r.reason ?? 'nothing can run this role now'}`;
}
