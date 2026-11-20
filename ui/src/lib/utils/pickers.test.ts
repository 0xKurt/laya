// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	EFFORT_PRESETS,
	addChips,
	addFolder,
	durationLabel,
	durationOptions,
	filterTimezones,
	normalizeEmail,
	normalizeReviewer,
	reviewerError,
	presetFor,
	removeChip,
	selectAll,
	senderError,
	splitChipInput,
	timeIn,
	toggleItem,
	unknownSelected
} from './pickers';

describe('checkbox lists', () => {
	it('toggles keeping the offered order', () => {
		const all = ['acme', 'beta', 'zeta'];
		expect(toggleItem(['zeta'], 'acme', all)).toEqual(['acme', 'zeta']);
		expect(toggleItem(['acme', 'zeta'], 'acme', all)).toEqual(['zeta']);
		expect(toggleItem(['old', 'zeta'], 'beta', all)).toEqual(['beta', 'zeta', 'old']);
		expect(toggleItem(['a'], 'b')).toEqual(['a', 'b']);
	});

	it('selects all without losing orgs no longer offered', () => {
		expect(selectAll(['a', 'b'], ['gone'])).toEqual(['a', 'b', 'gone']);
		expect(unknownSelected(['a', 'b'], ['b', 'gone'])).toEqual(['gone']);
	});
});

describe('sender chips', () => {
	it('normalizes and validates addresses', () => {
		expect(normalizeEmail('  Ann <ANN@Corp.io> ')).toBe('ann@corp.io');
		expect(senderError('ann@corp.io', [])).toBeNull();
		expect(senderError('ann', [])).toMatch(/not an email/);
		expect(senderError('ann@corp.io', ['ANN@corp.io'])).toMatch(/already/);
		expect(senderError('', [])).toMatch(/Enter/);
	});

	it('splits pasted lists and keeps "Name <address>" together', () => {
		expect(splitChipInput('Ann Lee <ann@x.io>, bob@x.io carol@x.io\ndave@x.io;')).toEqual([
			'Ann Lee <ann@x.io>',
			'bob@x.io',
			'carol@x.io',
			'dave@x.io'
		]);
	});

	it('adds valid chips, reports the first problem and keeps the bad part', () => {
		const r = addChips(['a@x.io'], 'b@x.io, nope, A@x.io');
		expect(r.list).toEqual(['a@x.io', 'b@x.io']);
		expect(r.error).toMatch(/"nope" is not an email/);
		expect(r.rest).toBe('nope A@x.io');
		expect(addChips([], '   ').error).toMatch(/Enter/);
		expect(removeChip(['a', 'b'], 'a')).toEqual(['b']);
	});
});

describe('reviewer chips', () => {
	it('accepts logins and org/team, drops @, rejects duplicates and junk', () => {
		const r = addChips(['octocat'], ' @octocat, acme/core  byron\nbad!', normalizeReviewer, reviewerError);
		expect(r.list).toEqual(['octocat', 'acme/core', 'byron']);
		expect(r.error).toMatch(/already/);
		expect(reviewerError('valid!', [])).toMatch(/not a GitHub login/);
		expect(reviewerError('', [])).toMatch(/Enter/);
	});
});

describe('time zones', () => {
	const zones = ['America/New_York', 'Europe/Berlin', 'Europe/London', 'UTC', 'America/Argentina/Buenos_Aires'];

	it('matches every word, city prefix first', () => {
		expect(filterTimezones(zones, 'new york')).toEqual(['America/New_York']);
		expect(filterTimezones(zones, 'europe')).toEqual(['Europe/Berlin', 'Europe/London']);
		expect(filterTimezones(zones, 'b')[0]).toBe('America/Argentina/Buenos_Aires');
		expect(filterTimezones(zones, '', 2)).toHaveLength(2);
		expect(filterTimezones(zones, 'xyz')).toEqual([]);
	});

	it('formats the time in a zone', () => {
		const at = new Date('2026-01-15T12:00:00Z');
		expect(timeIn('UTC', at)).toBe('12:00');
		expect(timeIn('Europe/Berlin', at)).toBe('13:00');
		expect(timeIn('Not/AZone', at)).toBe('');
	});
});

describe('effort presets', () => {
	it('finds the preset of a value', () => {
		expect(EFFORT_PRESETS.map((p) => p.label)).toEqual(['none', '15m', '30m', '1h', '2h', '½ day', '1 day']);
		expect(presetFor(240)?.label).toBe('½ day');
		expect(presetFor(45)).toBeNull();
		expect(presetFor(null)).toBeNull();
	});
});

describe('durations', () => {
	it('labels durations', () => {
		expect(durationLabel(30, 'minutes')).toBe('30 min');
		expect(durationLabel(60, 'minutes')).toBe('1 hour');
		expect(durationLabel(90, 'minutes')).toBe('1 h 30 min');
		expect(durationLabel(12, 'hours')).toBe('12 hours');
		expect(durationLabel(48, 'hours')).toBe('2 days');
		expect(durationLabel(168, 'hours')).toBe('1 week');
		expect(durationLabel(0, 'hours')).toBe('right away');
		expect(durationLabel(14, 'days')).toBe('2 weeks');
		expect(durationLabel(3, 'days')).toBe('3 days');
	});

	it('keeps a custom stored value selectable', () => {
		expect(durationOptions([15, 60], 45)).toEqual([15, 45, 60]);
		expect(durationOptions([15, 60], 60)).toEqual([15, 60]);
	});
});

describe('folder lists', () => {
	it('adds trimmed, unique paths', () => {
		expect(addFolder([], ' /Users/me/code/ ')).toEqual(['/Users/me/code']);
		expect(addFolder(['/a'], '/a/')).toEqual(['/a']);
		expect(addFolder(['/a'], '')).toEqual(['/a']);
		expect(addFolder([], '/')).toEqual(['/']);
	});
});
