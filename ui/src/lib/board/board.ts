// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Pure logic of the Board view (routes/board): which lane a card is in, the
// filters, and the engine calls a drag from one lane to another needs.
//
// Lanes come from Laya's own data (docs/claude-code-connector.md, "Board"):
// In progress = a running workspace agent or executor (status), or the system
// tag `lane:in-progress`; To do = system tag `lane:todo`; Done = status done;
// Backlog = every other card the board shows.

import type { ActionCard } from '$lib/api/types';
import { platformKey } from '$lib/utils/cardVisuals';
import { sumYouMinutes } from '$lib/sessions/session';

export type Lane = 'backlog' | 'todo' | 'in_progress' | 'done';
export type DropTarget = Lane | 'archive';

export const LANES: { id: Lane; title: string }[] = [
	{ id: 'backlog', title: 'Backlog' },
	{ id: 'todo', title: 'To do' },
	{ id: 'in_progress', title: 'In progress' },
	{ id: 'done', title: 'Done' }
];

export const LANE_TAG_NAMES = { todo: 'lane:todo', in_progress: 'lane:in-progress' } as const;
export type TaggedLane = keyof typeof LANE_TAG_NAMES;
/** Tag ids of the lane tags, as returned by GET /board. */
export type LaneTagIds = Record<TaggedLane, number>;

/** Statuses of a card that a workspace agent (Claude session) is working on. */
export const AGENT_SESSION_STATUSES: ReadonlySet<string> = new Set(['agent_running', 'awaiting_input']);
/** Statuses that put a card in In progress whatever its lane tags say. */
export const IN_PROGRESS_STATUSES: ReadonlySet<string> = new Set([
	'agent_running',
	'awaiting_input',
	'executing'
]);
/** Statuses the board never shows. */
const HIDDEN_STATUSES: ReadonlySet<string> = new Set(['dismissed', 'archived']);

const PRIORITY_RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

function hasTag(card: ActionCard, name: string): boolean {
	return !!card.tags?.some((t) => t.tag_name === name);
}

export function isAgentSession(card: ActionCard): boolean {
	return AGENT_SESSION_STATUSES.has(card.status);
}

export function laneOf(card: ActionCard): Lane {
	if (card.status === 'done') return 'done';
	if (IN_PROGRESS_STATUSES.has(card.status) || hasTag(card, LANE_TAG_NAMES.in_progress)) return 'in_progress';
	if (hasTag(card, LANE_TAG_NAMES.todo)) return 'todo';
	return 'backlog';
}

/** The card without its lane tags: the lane already shows them. */
export function withoutLaneTags(card: ActionCard): ActionCard {
	if (!card.tags?.some((t) => t.tag_name.startsWith('lane:'))) return card;
	return { ...card, tags: card.tags.filter((t) => !t.tag_name.startsWith('lane:')) };
}

// ── filters ──

export type BoardFilters = { search: string; priorities: string[]; platforms: string[] };

export const EMPTY_FILTERS: BoardFilters = { search: '', priorities: [], platforms: [] };

export function isFiltering(f: BoardFilters): boolean {
	return !!f.search.trim() || f.priorities.length > 0 || f.platforms.length > 0;
}

export function matchesFilters(card: ActionCard, f: BoardFilters): boolean {
	if (f.priorities.length && !f.priorities.includes(card.priority)) return false;
	if (f.platforms.length && !f.platforms.includes(platformKey(card.entity_id))) return false;
	const q = f.search.trim().toLowerCase();
	if (!q) return true;
	return [card.header, card.summary, card.actor_name, card.source_context].some((text) =>
		text?.toLowerCase().includes(q)
	);
}

/** The sources that have at least one card, sorted: one filter chip each. */
export function presentPlatforms(cards: ActionCard[]): string[] {
	return [...new Set(cards.map((c) => platformKey(c.entity_id)))].filter(Boolean).sort();
}

/** Visible cards per lane. Open lanes sort by priority, then newest; Done by newest. */
export function groupByLane(cards: ActionCard[], f: BoardFilters): Record<Lane, ActionCard[]> {
	const byLane: Record<Lane, ActionCard[]> = { backlog: [], todo: [], in_progress: [], done: [] };
	for (const card of cards) {
		if (HIDDEN_STATUSES.has(card.status) || !matchesFilters(card, f)) continue;
		byLane[laneOf(card)].push(card);
	}
	const newest = (a: ActionCard, b: ActionCard) => (b.created_at ?? '').localeCompare(a.created_at ?? '');
	for (const lane of ['backlog', 'todo', 'in_progress'] as const) {
		byLane[lane].sort((a, b) => (PRIORITY_RANK[a.priority] ?? 9) - (PRIORITY_RANK[b.priority] ?? 9) || newest(a, b));
	}
	byLane.done.sort((a, b) => (b.resolved_at ?? b.updated_at ?? '').localeCompare(a.resolved_at ?? a.updated_at ?? '') || newest(a, b));
	return byLane;
}

/**
 * The user's own estimated minutes per lane (the "you" part of each card's
 * effort; cards without an estimate count 0). Done shows what was spent.
 */
export function youMinutesByLane(byLane: Record<Lane, ActionCard[]>): Record<Lane, number> {
	return {
		backlog: sumYouMinutes(byLane.backlog),
		todo: sumYouMinutes(byLane.todo),
		in_progress: sumYouMinutes(byLane.in_progress),
		done: sumYouMinutes(byLane.done)
	};
}

// ── moves ──

/** One engine call of a move, in the order it must run. */
export type MoveStep =
	| { kind: 'archive' }
	| { kind: 'done' }
	| { kind: 'reopen' }
	| { kind: 'assign'; lane: TaggedLane }
	| { kind: 'unassign'; lane: TaggedLane };

/**
 * The engine calls that move `card` to `target`. Empty when the card is
 * already there.
 *
 * Lane tags are cleared when a card leaves Backlog/To do/In progress for Done
 * or Archive, so a reopened card does not jump back into an old lane. A card
 * whose status keeps it In progress (a running agent) can still be tagged for
 * another lane; it shows there once the agent finishes.
 */
export function planMove(card: ActionCard, target: DropTarget): MoveStep[] {
	if (target !== 'archive' && laneOf(card) === target) return [];
	const tagged = (lane: TaggedLane) => hasTag(card, LANE_TAG_NAMES[lane]);
	const clearTags = (keep?: TaggedLane): MoveStep[] =>
		(['todo', 'in_progress'] as const)
			.filter((lane) => lane !== keep && tagged(lane))
			.map((lane) => ({ kind: 'unassign', lane }));

	if (target === 'archive') return [{ kind: 'archive' }, ...clearTags()];
	if (target === 'done') return [{ kind: 'done' }, ...clearTags()];

	const steps: MoveStep[] = card.status === 'done' ? [{ kind: 'reopen' }] : [];
	if (target === 'backlog') return [...steps, ...clearTags()];
	steps.push(...clearTags(target));
	if (!tagged(target)) steps.push({ kind: 'assign', lane: target });
	return steps;
}

/** The card as it looks after `planMove(card, target)` succeeded (for an optimistic update). */
export function applyMove(card: ActionCard, target: Lane, laneTagIds: LaneTagIds): ActionCard {
	const tags = (card.tags ?? []).filter((t) => !t.tag_name.startsWith('lane:'));
	if (target === 'todo' || target === 'in_progress') {
		tags.push({
			tag_id: laneTagIds[target],
			tag_name: LANE_TAG_NAMES[target],
			is_system: true,
			assigned_by: 'user'
		});
	}
	const status = target === 'done' ? 'done' : card.status === 'done' ? 'ready' : card.status;
	return { ...card, status, tags };
}
