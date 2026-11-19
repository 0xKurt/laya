// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { cardReference, linearReference, oneLine, prReference } from './reference';

describe('Claude references', () => {
	it('builds a card reference from platform, header and source url', () => {
		expect(
			cardReference({ entity_id: 'slack:thread:C1:2', header: 'Deploy\nquestion', source_url: 'https://s/x' })
		).toBe('[slack card] Deploy question — https://s/x');
	});

	it('leaves the url out when there is none', () => {
		expect(cardReference({ entity_id: undefined, header: 'Note', source_url: undefined })).toBe('[Laya card] Note');
		expect(linearReference({ identifier: 'TRAD-1', title: 'Fix', url: null })).toBe('[TRAD-1] Fix');
	});

	it('builds Linear and PR references', () => {
		expect(linearReference({ identifier: 'TRAD-12', title: ' Add  X ', url: 'https://linear.app/t/TRAD-12' })).toBe(
			'[TRAD-12] Add X — https://linear.app/t/TRAD-12'
		);
		expect(prReference({ repo: 'somnia/laya', number: 7, title: 'Terminal', url: 'https://github.com/somnia/laya/pull/7' })).toBe(
			'[somnia/laya#7] Terminal — https://github.com/somnia/laya/pull/7'
		);
	});

	it('collapses every kind of line break', () => {
		expect(oneLine('a\r\nb\tc d')).toBe('a b c d');
	});
});
