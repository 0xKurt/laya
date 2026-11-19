// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import { VIEWS, normalizeHidden, visibleViews, landingPath, isHrefHidden, canHide, setVisible, type ViewId } from './views';

const ALL = VIEWS.map((v) => v.id);

describe('normalizeHidden', () => {
	it('returns [] for missing or invalid values', () => {
		expect(normalizeHidden(undefined)).toEqual([]);
		expect(normalizeHidden('omni')).toEqual([]);
	});
	it('drops unknown ids and duplicates', () => {
		expect(normalizeHidden(['omni', 'nope', 'omni', 3, 'corpus'])).toEqual(['omni', 'corpus']);
	});
	it('treats hiding every view as hiding nothing', () => {
		expect(normalizeHidden(ALL)).toEqual([]);
	});
});

describe('visibleViews', () => {
	it('keeps navigation order and drops hidden views', () => {
		expect(visibleViews(['board', 'omni']).map((v) => v.label)).toEqual([
			'Pulse',
			'My work',
			'Claude',
			'Coherence',
			'Corpus'
		]);
	});
	it('shows everything by default', () => {
		expect(visibleViews([])).toHaveLength(VIEWS.length);
	});
});

describe('landingPath', () => {
	it('defaults to Board', () => {
		expect(landingPath([])).toBe('/board');
	});
	it('follows Board, My work, Claude, Pulse, Omni, Coherence, Corpus', () => {
		expect(landingPath(['board'])).toBe('/work');
		expect(landingPath(['board', 'work'])).toBe('/terminal');
		expect(landingPath(['board', 'work', 'claude'])).toBe('/feed');
		expect(landingPath(['board', 'work', 'claude', 'pulse'])).toBe('/omni');
		expect(landingPath(['board', 'work', 'claude', 'pulse', 'omni'])).toBe('/coherence');
		expect(landingPath(['board', 'work', 'claude', 'pulse', 'omni', 'coherence'])).toBe('/corpus');
	});
});

describe('isHrefHidden', () => {
	it('matches view paths only', () => {
		expect(isHrefHidden(['omni'], '/omni')).toBe(true);
		expect(isHrefHidden(['omni'], '/feed')).toBe(false);
		expect(isHrefHidden(['omni'], '/settings')).toBe(false);
	});
});

describe('at least one visible view', () => {
	const allButCorpus = ALL.filter((v) => v !== 'corpus') as ViewId[];
	it('canHide is false for the last visible view', () => {
		expect(canHide(allButCorpus, 'corpus')).toBe(false);
		expect(canHide(allButCorpus, 'omni')).toBe(true);
		expect(canHide([], 'omni')).toBe(true);
	});
	it('setVisible refuses to hide the last visible view', () => {
		expect(setVisible(allButCorpus, 'corpus', false)).toEqual(allButCorpus);
	});
	it('setVisible hides and shows in navigation order', () => {
		expect(setVisible(['omni'], 'pulse', false)).toEqual(['pulse', 'omni']);
		expect(setVisible(['pulse', 'omni'], 'omni', true)).toEqual(['pulse']);
		expect(setVisible(['omni'], 'omni', false)).toEqual(['omni']);
	});
});
