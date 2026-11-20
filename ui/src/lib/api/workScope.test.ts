import { describe, expect, it } from 'vitest';
import { scopeLabel } from './workScope';

describe('scopeLabel', () => {
	it('lists the orgs, or says everything when none is set', () => {
		expect(scopeLabel(['a', 'b'])).toBe('a, b');
		expect(scopeLabel([])).toBe('all repositories');
		expect(scopeLabel(null)).toBe('all repositories');
	});
});
