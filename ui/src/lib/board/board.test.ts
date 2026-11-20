// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import type { TagAssignment } from '$lib/api/types';
import {
	EMPTY_FILTERS,
	applyMove,
	groupByLane,
	isFiltering,
	laneOf,
	matchesFilters,
	mergeVisibleOrder,
	planMove,
	withLaneOrder,
	presentPlatforms,
	withoutLaneTags,
	youMinutesByLane,
	type BoardCard
} from './board';

const TODO: TagAssignment = { tag_id: 11, tag_name: 'lane:todo', is_system: true, assigned_by: 'user' };
const DOING: TagAssignment = { tag_id: 12, tag_name: 'lane:in-progress', is_system: true, assigned_by: 'user' };
const BUG: TagAssignment = { tag_id: 3, tag_name: 'bug', is_system: false, assigned_by: 'stager' };
const IDS = { todo: 11, in_progress: 12 };

function card(over: Partial<BoardCard> = {}): BoardCard {
	return {
		card_id: 'c1',
		event_id: 'e1',
		priority: 'MEDIUM',
		persona: 'ENGINEER',
		category: 'CODE',
		header: 'Fix login',
		summary: 'Login fails on Safari',
		status: 'ready',
		privacy_tier: 2,
		has_workspace: false,
		entity_id: 'jira:ticket:ABC-1',
		created_at: '2026-11-10 10:00:00',
		tags: [],
		...over
	};
}

describe('laneOf', () => {
	it('puts an untagged active card in Backlog', () => {
		expect(laneOf(card())).toBe('backlog');
		expect(laneOf(card({ status: 'failed', tags: [BUG] }))).toBe('backlog');
	});
	it('uses the lane tags', () => {
		expect(laneOf(card({ tags: [TODO] }))).toBe('todo');
		expect(laneOf(card({ tags: [DOING] }))).toBe('in_progress');
		expect(laneOf(card({ tags: [TODO, DOING] }))).toBe('in_progress');
	});
	it('puts running agents and executions in In progress whatever the tags', () => {
		for (const status of ['agent_running', 'awaiting_input', 'executing'] as const) {
			expect(laneOf(card({ status, tags: [TODO] }))).toBe('in_progress');
		}
	});
	it('puts done cards in Done even with a lane tag', () => {
		expect(laneOf(card({ status: 'done', tags: [DOING] }))).toBe('done');
	});
});

describe('filters', () => {
	const cards = [
		card({ card_id: 'a', priority: 'HIGH', actor_name: 'Ana', entity_id: 'slack:msg:1', source_context: '#ops' }),
		card({ card_id: 'b', priority: 'LOW', header: 'Invoice', summary: 'Pay it', entity_id: 'gmail:msg:2' })
	];
	it('matches search over title, summary, actor and channel', () => {
		const ids = (search: string) =>
			cards.filter((c) => matchesFilters(c, { ...EMPTY_FILTERS, search })).map((c) => c.card_id);
		expect(ids('invoice')).toEqual(['b']);
		expect(ids('PAY')).toEqual(['b']);
		expect(ids('ana')).toEqual(['a']);
		expect(ids('#ops')).toEqual(['a']);
		expect(ids('  ')).toEqual(['a', 'b']);
	});
	it('filters by priority and source', () => {
		expect(cards.filter((c) => matchesFilters(c, { ...EMPTY_FILTERS, priorities: ['HIGH'] })).map((c) => c.card_id)).toEqual(['a']);
		expect(cards.filter((c) => matchesFilters(c, { ...EMPTY_FILTERS, platforms: ['gmail'] })).map((c) => c.card_id)).toEqual(['b']);
	});
	it('lists the sources present', () => {
		expect(presentPlatforms([...cards, card({ entity_id: undefined })])).toEqual(['gmail', 'slack']);
	});
	it('knows when a filter is active', () => {
		expect(isFiltering(EMPTY_FILTERS)).toBe(false);
		expect(isFiltering({ ...EMPTY_FILTERS, search: 'x' })).toBe(true);
	});
});

describe('groupByLane', () => {
	it('drops dismissed and archived cards and sorts open lanes by priority then newest', () => {
		const lanes = groupByLane(
			[
				card({ card_id: 'low-new', priority: 'LOW', created_at: '2026-11-12' }),
				card({ card_id: 'high-old', priority: 'HIGH', created_at: '2026-11-01' }),
				card({ card_id: 'med-new', created_at: '2026-11-12' }),
				card({ card_id: 'med-old', created_at: '2026-11-02' }),
				card({ card_id: 'gone', status: 'dismissed' }),
				card({ card_id: 'arch', status: 'archived' }),
				card({ card_id: 'd', status: 'done' })
			],
			EMPTY_FILTERS
		);
		expect(lanes.backlog.map((c) => c.card_id)).toEqual(['high-old', 'med-new', 'med-old', 'low-new']);
		expect(lanes.done.map((c) => c.card_id)).toEqual(['d']);
	});
});

describe('manual order', () => {
	const ids = (cards: { card_id: string }[]) => cards.map((c) => c.card_id);

	it('puts unplaced cards on top (priority, then latest activity), then placed cards by position', () => {
		const lanes = groupByLane(
			[
				card({ card_id: 'p2', lane: 'backlog', position: 2 }),
				card({ card_id: 'p0', lane: 'backlog', position: 0, priority: 'LOW' }),
				card({ card_id: 'new-med', created_at: '2026-11-12' }),
				card({ card_id: 'new-med-active', created_at: '2026-11-01', group_active_at: '2026-11-15' }),
				card({ card_id: 'new-crit', priority: 'CRITICAL', created_at: '2026-11-01' }),
				card({ card_id: 'p1', lane: 'backlog', position: 1, priority: 'CRITICAL' })
			],
			EMPTY_FILTERS
		);
		expect(ids(lanes.backlog)).toEqual(['new-crit', 'new-med-active', 'new-med', 'p0', 'p1', 'p2']);
	});

	it('ignores a position from another lane (the card moved by a status change)', () => {
		const lanes = groupByLane(
			[
				card({ card_id: 'placed', tags: [TODO], lane: 'todo', position: 0 }),
				card({ card_id: 'stale', tags: [TODO], lane: 'backlog', position: 5, priority: 'LOW' })
			],
			EMPTY_FILTERS
		);
		expect(ids(lanes.todo)).toEqual(['stale', 'placed']);
	});

	it('keeps Done newest first whatever the positions', () => {
		const lanes = groupByLane(
			[
				card({ card_id: 'old', status: 'done', resolved_at: '2026-11-10', lane: 'done', position: 0 }),
				card({ card_id: 'new', status: 'done', resolved_at: '2026-11-12', lane: 'done', position: 1 })
			],
			EMPTY_FILTERS
		);
		expect(ids(lanes.done)).toEqual(['new', 'old']);
	});

	it('places cards optimistically', () => {
		const moved = withLaneOrder([card({ card_id: 'a' }), card({ card_id: 'b' }), card({ card_id: 'c' })], 'backlog', ['c', 'a']);
		expect(moved.map((c) => [c.card_id, c.position ?? null])).toEqual([['a', 1], ['b', null], ['c', 0]]);
		expect(ids(groupByLane(moved, EMPTY_FILTERS).backlog)).toEqual(['b', 'c', 'a']);
	});

	it('merges a filtered drop back into the full lane order', () => {
		// Visible: a, c, e. Hidden: b (after a), d (after c), x (on top).
		const lane = ['x', 'a', 'b', 'c', 'd', 'e'];
		expect(mergeVisibleOrder(lane, ['a', 'c', 'e'])).toEqual(lane);
		expect(mergeVisibleOrder(lane, ['c', 'a', 'e'])).toEqual(['x', 'c', 'd', 'a', 'b', 'e']);
		// A card dropped in from another lane.
		expect(mergeVisibleOrder(lane, ['a', 'n', 'c', 'e'])).toEqual(['x', 'a', 'b', 'n', 'c', 'd', 'e']);
		// A card that left the lane is not in `laneIds`; its hidden follower stays in place.
		expect(mergeVisibleOrder(['x', 'b', 'c', 'd', 'e'], ['e', 'c'])).toEqual(['x', 'b', 'e', 'c', 'd']);
	});
});

describe('planMove', () => {
	it('does nothing when the card is already in the lane', () => {
		expect(planMove(card({ tags: [TODO] }), 'todo')).toEqual([]);
	});
	it('tags a backlog card for To do / In progress', () => {
		expect(planMove(card(), 'todo')).toEqual([{ kind: 'assign', lane: 'todo' }]);
		expect(planMove(card({ tags: [TODO] }), 'in_progress')).toEqual([
			{ kind: 'unassign', lane: 'todo' },
			{ kind: 'assign', lane: 'in_progress' }
		]);
	});
	it('removes the lane tags when moved back to Backlog', () => {
		expect(planMove(card({ tags: [DOING, BUG] }), 'backlog')).toEqual([{ kind: 'unassign', lane: 'in_progress' }]);
	});
	it('marks done and clears lane tags', () => {
		expect(planMove(card({ tags: [TODO] }), 'done')).toEqual([{ kind: 'done' }, { kind: 'unassign', lane: 'todo' }]);
	});
	it('reopens a done card before tagging it', () => {
		expect(planMove(card({ status: 'done' }), 'todo')).toEqual([{ kind: 'reopen' }, { kind: 'assign', lane: 'todo' }]);
		expect(planMove(card({ status: 'done' }), 'backlog')).toEqual([{ kind: 'reopen' }]);
	});
	it('archives from any lane', () => {
		expect(planMove(card({ status: 'done' }), 'archive')).toEqual([{ kind: 'archive' }]);
		expect(planMove(card({ tags: [DOING] }), 'archive')).toEqual([{ kind: 'archive' }, { kind: 'unassign', lane: 'in_progress' }]);
	});
});

describe('applyMove / withoutLaneTags', () => {
	it('moves the card optimistically', () => {
		const moved = applyMove(card({ status: 'done', tags: [BUG] }), 'in_progress', IDS);
		expect(moved.status).toBe('ready');
		expect(laneOf(moved)).toBe('in_progress');
		expect(moved.tags?.map((t) => t.tag_name)).toEqual(['bug', 'lane:in-progress']);
		expect(laneOf(applyMove(card({ tags: [TODO] }), 'done', IDS))).toBe('done');
		expect(laneOf(applyMove(card({ tags: [TODO] }), 'backlog', IDS))).toBe('backlog');
	});
	it('hides lane tags from the card display', () => {
		expect(withoutLaneTags(card({ tags: [TODO, BUG] })).tags).toEqual([BUG]);
	});
});

describe('youMinutesByLane', () => {
	it('sums the user part of each card estimate per lane', () => {
		const lanes = groupByLane(
			[
				card({ card_id: 'a', effort: { you_minutes: 30, agent_minutes: 120 } }),
				card({ card_id: 'b', effort: { you_minutes: 15, agent_minutes: 0 } }),
				card({ card_id: 'c', tags: [TODO], effort: { you_minutes: 60, agent_minutes: 30 } }),
				card({ card_id: 'd', tags: [TODO] })
			],
			EMPTY_FILTERS
		);
		expect(youMinutesByLane(lanes)).toEqual({ backlog: 45, todo: 60, in_progress: 0, done: 0 });
	});
});
