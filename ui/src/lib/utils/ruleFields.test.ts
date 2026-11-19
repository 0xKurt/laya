// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import { conditionValue, isFieldPath, ruleNameParts, valueSummary, valueText } from './ruleFields';

describe('isFieldPath', () => {
	it('accepts dot paths and rejects anything else', () => {
		expect(isFieldPath('content.metadata.github_reason')).toBe(true);
		expect(isFieldPath('actor.platform_handle')).toBe(true);
		expect(isFieldPath('')).toBe(false);
		expect(isFieldPath('content..body')).toBe(false);
		expect(isFieldPath('content.metadata.')).toBe(false);
		expect(isFieldPath('a b')).toBe(false);
	});
});

describe('condition values', () => {
	it('keeps `in` a list through edit and save', () => {
		const seeded = ['review_requested', 'state_change', 'ci_activity'];
		expect(conditionValue('in', valueText(seeded))).toEqual(seeded);
		expect(conditionValue('in', ' a, ,b ')).toEqual(['a', 'b']);
	});
	it('leaves other operators as text', () => {
		expect(conditionValue('ends_with', '[bot]')).toBe('[bot]');
	});
	it('summarises lists and strings', () => {
		expect(valueSummary(['a', 'b'])).toBe('[a, b]');
		expect(valueSummary('github')).toBe('"github"');
	});
});

describe('ruleNameParts', () => {
	it('splits the seeded prefix off', () => {
		expect(ruleNameParts('Claude Code · Ignore bot comments')).toEqual({
			origin: 'Claude Code',
			title: 'Ignore bot comments'
		});
		expect(ruleNameParts('Mute #random')).toEqual({ origin: null, title: 'Mute #random' });
	});
});
