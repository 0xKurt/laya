import { describe, expect, it } from 'vitest';
import { agentModelOptions, collectModelGroups, describeAutoRole, normalizeCollectModel } from './modelAccess';

describe('describeAutoRole', () => {
	it('names the agent, login and model a role runs on now', () => {
		expect(
			describeAutoRole({
				automatic: true,
				via: 'agent',
				agent_id: 'claude_code',
				agent_label: 'Claude Code',
				login_label: 'me@example.com',
				model: 'agent/claude_code/haiku'
			})
		).toBe('Automatic · now on Claude Code · me@example.com · haiku');
		expect(describeAutoRole({ automatic: true, via: 'agent', agent_label: 'Codex', model: 'agent/codex_cli' })).toBe(
			'Automatic · now on Codex · default model'
		);
	});
	it('says when the API fallback or nothing runs it', () => {
		expect(describeAutoRole({ automatic: true, via: 'api', model: 'gpt-4o' })).toMatch(/API model gpt-4o/);
		expect(describeAutoRole({ automatic: true, via: 'none', reason: 'No coding agent login with room.' })).toBe(
			'Automatic · No coding agent login with room.'
		);
	});
});

describe('collector model helpers', () => {
	const groups = [
		{ agent_id: 'claude_code', label: 'Claude Code', models: [{ value: 'agent/claude_code/haiku', label: 'Haiku' }] }
	];
	it('reads a stored bare name as a Claude Code model and empty as Automatic', () => {
		expect(normalizeCollectModel('haiku')).toBe('agent/claude_code/haiku');
		expect(normalizeCollectModel('')).toBe('agent/auto');
		expect(normalizeCollectModel(undefined)).toBe('agent/auto');
		expect(normalizeCollectModel('agent/codex_cli/gpt-x')).toBe('agent/codex_cli/gpt-x');
	});
	it('keeps a stored model of an agent without a login visible', () => {
		expect(collectModelGroups(groups, 'haiku')).toEqual(groups);
		expect(collectModelGroups(groups, 'agent/auto')).toEqual(groups);
		const out = collectModelGroups(groups, 'agent/codex_cli/gpt-x');
		expect(out).toHaveLength(2);
		expect(out[1].models).toEqual([{ value: 'agent/codex_cli/gpt-x', label: 'gpt-x' }]);
	});
	it('offers the agent default, the known models and a custom current value', () => {
		const known = [{ value: 'sonnet', label: 'Sonnet' }];
		expect(agentModelOptions(known, '').map((o) => o.value)).toEqual(['', 'sonnet']);
		expect(agentModelOptions(known, 'my-model').map((o) => o.value)).toEqual(['', 'sonnet', 'my-model']);
	});
});
