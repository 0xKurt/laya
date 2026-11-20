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

// ── model pickers (GET /claude-code/agent-models) ──────────────────────

export interface ModelOption {
	value: string;
	label: string;
}

/** Explicit models of one coding agent that has a login. */
export interface ModelGroup {
	agent_id: string;
	label: string;
	models: ModelOption[];
}

export interface AgentModels {
	/** Plain model names each agent takes (empty: only the agent's default). */
	agents: Record<string, ModelOption[]>;
	/** Collector models (`agent/<id>/<model>`), for agents with a login. */
	collect: ModelGroup[];
	automatic: string;
}

let agentModels: Promise<AgentModels> | null = null;

/** The model lists, read once per page load (`refresh` reads them again). */
export function getAgentModels(refresh = false): Promise<AgentModels> {
	if (refresh || !agentModels) {
		agentModels = json<AgentModels>('/claude-code/agent-models', { signal: AbortSignal.timeout(60_000) });
		agentModels.catch(() => (agentModels = null));
	}
	return agentModels;
}

/**
 * A source's stored collector model in its current form: empty → Automatic,
 * a bare name (`haiku`) → that Claude Code model.
 */
export function normalizeCollectModel(value: string | null | undefined): string {
	const v = (value ?? '').trim();
	if (!v || v === AUTO_MODEL) return AUTO_MODEL;
	return v.startsWith('agent/') ? v : `agent/claude_code/${v}`;
}

/**
 * The groups a collector model select shows: the eligible agents' models,
 * plus the stored value when it is not among them (e.g. its agent's login was
 * removed), so the select never shows a value it cannot display.
 */
export function collectModelGroups(groups: ModelGroup[], current: string): ModelGroup[] {
	const v = normalizeCollectModel(current);
	if (v === AUTO_MODEL || groups.some((g) => g.models.some((m) => m.value === v))) return groups;
	const [agentId, ...rest] = v.slice('agent/'.length).split('/');
	const model = rest.join('/') || 'default model';
	return [...groups, { agent_id: agentId, label: `${agentId} (no login now)`, models: [{ value: v, label: model }] }];
}

/**
 * Options for an agent role's model select: the agent's default, its known
 * models, and the current value when it is a custom one.
 */
export function agentModelOptions(known: ModelOption[], current: string): ModelOption[] {
	const options = [{ value: '', label: 'Agent default' }, ...known];
	if (current && !known.some((m) => m.value === current)) options.push({ value: current, label: current });
	return options;
}
