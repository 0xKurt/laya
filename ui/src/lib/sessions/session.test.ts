// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import type { ActionCard, SessionClaim } from '$lib/api/types';
import {
	claimLines,
	effortOf,
	formatEffort,
	formatMinutes,
	mentionsClaim,
	parseDuration,
	sumYouMinutes
} from './session';

function card(over: Partial<ActionCard> = {}): ActionCard {
	return {
		card_id: 'c',
		event_id: 'e',
		priority: 'MEDIUM',
		persona: 'ENGINEER',
		category: 'CODE',
		header: 'h',
		summary: 's',
		status: 'ready',
		privacy_tier: 2,
		has_workspace: false,
		...over
	} as ActionCard;
}

describe('durations', () => {
	it('formats minutes', () => {
		expect(formatMinutes(15)).toBe('15m');
		expect(formatMinutes(120)).toBe('2h');
		expect(formatMinutes(90)).toBe('1h30m');
		expect(formatEffort({ you_minutes: 30, agent_minutes: 120 })).toBe('you ~30m · agent ~2h');
	});

	it('parses what the user types', () => {
		expect(parseDuration('45')).toBe(45);
		expect(parseDuration('45m')).toBe(45);
		expect(parseDuration('2h')).toBe(120);
		expect(parseDuration('1h 30m')).toBe(90);
		expect(parseDuration('1.5h')).toBe(90);
		expect(parseDuration('')).toBeNull();
		expect(parseDuration('soon')).toBeNull();
		expect(parseDuration('h')).toBeNull();
	});
});

describe('effort', () => {
	it('reads the copy on the card, else staged_output', () => {
		expect(effortOf(card({ effort: { you_minutes: 1, agent_minutes: 2 } }))?.you_minutes).toBe(1);
		expect(
			effortOf(card({ staged_output: { type: 'summary', content: '', effort: { you_minutes: 3, agent_minutes: 0 } } }))
				?.you_minutes
		).toBe(3);
		expect(effortOf(card())).toBeNull();
	});

	it('sums the user minutes of a lane', () => {
		const cards = [
			card({ effort: { you_minutes: 30, agent_minutes: 120 } }),
			card({ effort: { you_minutes: 15, agent_minutes: 0 } }),
			card()
		];
		expect(sumYouMinutes(cards)).toBe(45);
		expect(sumYouMinutes([])).toBe(0);
	});
});

describe('session claims', () => {
	const base: SessionClaim = {
		repo: 'o/r',
		number: 7,
		claims: ['merged', 'green'],
		url: null,
		state: 'OPEN',
		checks: 'failing',
		results: {},
		error: null
	};

	it('shows only confirmed claims as facts', () => {
		expect(claimLines({ ...base, state: 'MERGED', checks: 'green', results: { merged: 'confirmed', green: 'confirmed' } })).toEqual([
			{ text: 'o/r#7 merged (checked with gh)', tone: 'fact' },
			{ text: 'o/r#7 checks green (checked with gh)', tone: 'fact' }
		]);
		expect(claimLines({ ...base, results: { merged: 'contradicted', green: 'contradicted' } })).toEqual([
			{ text: 'o/r#7: session says merged, gh says open', tone: 'conflict' },
			{ text: 'o/r#7: session says checks green, gh says checks failing', tone: 'conflict' }
		]);
		expect(claimLines({ ...base, repo: null, number: null, claims: ['deployed'], results: { deployed: 'unverified' } })).toEqual([
			{ text: 'deployed: reported by the session, unverified', tone: 'lead' }
		]);
	});

	it('spots claim words', () => {
		expect(mentionsClaim('PR merged')).toBe(true);
		expect(mentionsClaim('CI is passing')).toBe(true);
		expect(mentionsClaim('needs review')).toBe(false);
		expect(mentionsClaim(undefined)).toBe(false);
	});
});
