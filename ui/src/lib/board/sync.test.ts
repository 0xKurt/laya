// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import { syncErrors, syncLabel, syncProgress, type SyncAllStatus } from './sync';

function status(over: Partial<SyncAllStatus> = {}): SyncAllStatus {
	return {
		running: false,
		started_at: null,
		finished_at: null,
		results: {},
		platforms: ['slack', 'github'],
		last_ok_at: null,
		next_at: null,
		budget_paused: false,
		...over
	};
}

describe('syncLabel', () => {
	const now = new Date(2026, 10, 20, 12, 0);
	const iso = (d: Date) => d.toISOString();

	it('says when the sources last synced and sync next', () => {
		const label = syncLabel(
			status({ last_ok_at: iso(new Date(2026, 10, 20, 11, 48)), next_at: iso(new Date(2026, 10, 20, 12, 48)) }),
			now
		);
		expect(label).toBe('Last synced 12 min ago · next ~12:48');
	});

	it('reads the engine timestamps without a zone as UTC', () => {
		const label = syncLabel(status({ last_ok_at: iso(new Date(2026, 10, 20, 9, 0)).replace('Z', '') }), now);
		expect(label).toBe('Last synced 3 h ago');
	});

	it('names the day of a next sync on another day', () => {
		const label = syncLabel(status({ next_at: iso(new Date(2026, 10, 23, 8, 0)) }), now);
		expect(label).toMatch(/^Not synced yet · next ~\S+ 08:00$/);
	});

	it('is empty without enabled sources', () => {
		expect(syncLabel(status({ platforms: [] }), now)).toBe('');
		expect(syncLabel(null, now)).toBe('');
	});
});

describe('progress and errors', () => {
	it('counts finished sources and lists failures with their labels', () => {
		const s = status({
			running: true,
			results: { slack: { events: 0, error: 'MCP down' }, github: { events: 3, error: null } }
		});
		expect(syncProgress(s)).toBe('2 of 2 sources');
		expect(syncErrors(s, { slack: 'Slack' })).toEqual(['Slack: MCP down']);
		expect(syncErrors(null)).toEqual([]);
	});
});
