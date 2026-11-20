import { describe, expect, it } from 'vitest';
import { describeAutoRole } from './modelAccess';

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
