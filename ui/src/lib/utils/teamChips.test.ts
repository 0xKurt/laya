import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api/engine', () => ({ engineApi: {} }));

import { aliasError, identitySuggestions } from './teamChips';

describe('aliasError', () => {
	it('accepts a new email and refuses others', () => {
		expect(aliasError('a@x.io', [])).toBeNull();
		expect(aliasError('nope', [])).toMatch(/not an email/);
		expect(aliasError('a@x.io', ['A@x.io'])).toMatch(/already added/);
	});
});

describe('identitySuggestions', () => {
	it('offers the accounts and emails the sources signed in as, with where they come from', () => {
		const s = identitySuggestions({
			gmail: { email: 'me@x.io', name: 'Me' },
			slack: { email: 'me@work.io', slack_user_id: 'U123' },
			github: { github_login: 'octo' }
		});
		expect(s.accounts.map((c) => c.value)).toEqual(['U123', 'octo']);
		expect(s.accounts[0].label).toBe('U123 · slack');
		expect(s.aliases.map((c) => c.value)).toEqual(['me@x.io', 'me@work.io']);
	});
	it('is empty without identities', () => {
		expect(identitySuggestions({})).toEqual({ accounts: [], aliases: [] });
	});
});
