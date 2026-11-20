// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Pure logic of the Board view (routes/board): which lane a card is in, the
// filters, the order of the cards in a lane, and the engine calls a drag from
// one lane to another needs.
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
/** Lanes the user orders by hand: all of them. */
export type OrderedLane = Lane;

/**
 * A card as GET /board returns it: `lane` is the lane the engine computed,
 * `position` its manual place in that lane (null until the user placed it).
 */
export type BoardCard = ActionCard & { lane?: Lane; position?: number | null };

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

/** The latest activity of a card (its group, its source event, or its creation), as a sortable string. */
export function lastActivity(card: ActionCard): string {
	return [card.group_active_at, card.event_timestamp, card.created_at].reduce<string>(
		(max, t) => (t && t > max ? t : max),
		''
	);
}

/** The card's manual position, if it was placed in the lane it is in now. */
export function positionOf(card: BoardCard): number | null {
	if (card.position == null || card.lane !== laneOf(card)) return null;
	return card.position;
}

/** Default order of cards without a position: Done newest first, the other lanes by priority, then latest activity. */
function compareDefault(a: BoardCard, b: BoardCard): number {
	if (a.status === 'done' && b.status === 'done') {
		const doneAt = (c: ActionCard) => c.resolved_at ?? c.updated_at ?? '';
		return doneAt(b).localeCompare(doneAt(a)) || (b.created_at ?? '').localeCompare(a.created_at ?? '');
	}
	return (
		(PRIORITY_RANK[a.priority] ?? 9) - (PRIORITY_RANK[b.priority] ?? 9) ||
		lastActivity(b).localeCompare(lastActivity(a))
	);
}

/**
 * Order inside a lane: cards without a position first (default order), then
 * the placed cards by position. The engine gives newcomers in a lane with a
 * manual order a stored place on top, so "first" holds only until the next load.
 */
export function compareInLane(a: BoardCard, b: BoardCard): number {
	const pa = positionOf(a);
	const pb = positionOf(b);
	if (pa == null && pb == null) return compareDefault(a, b);
	if (pa == null) return -1;
	if (pb == null) return 1;
	return pa - pb;
}

/** Visible cards per lane, each in its manual order. */
export function groupByLane<T extends BoardCard>(cards: T[], f: BoardFilters): Record<Lane, T[]> {
	const byLane: Record<Lane, T[]> = { backlog: [], todo: [], in_progress: [], done: [] };
	for (const card of cards) {
		if (HIDDEN_STATUSES.has(card.status) || !matchesFilters(card, f)) continue;
		byLane[laneOf(card)].push(card);
	}
	for (const lane of Object.keys(byLane) as Lane[]) byLane[lane].sort(compareInLane);
	return byLane;
}

/**
 * The full order of a lane after a drop, when only some of its cards were
 * visible (a filter is on). `laneIds` is the lane's full order before the
 * drop without a card that left it, `visibleIds` the visible cards in their
 * new order (a card dropped in included). Each hidden card stays right after the visible card it followed;
 * hidden cards before the first visible card stay on top.
 */
export function mergeVisibleOrder(laneIds: string[], visibleIds: string[]): string[] {
	const visible = new Set(visibleIds);
	const leading: string[] = [];
	const followers = new Map<string, string[]>();
	let anchor: string | null = null;
	for (const id of laneIds) {
		if (visible.has(id)) {
			anchor = id;
			continue;
		}
		if (anchor == null) leading.push(id);
		else followers.set(anchor, [...(followers.get(anchor) ?? []), id]);
	}
	return [...leading, ...visibleIds.flatMap((id) => [id, ...(followers.get(id) ?? [])])];
}

/** What a drop of a card into a lane needs: the move steps and the lane's new full order. */
export type DropPlan = { steps: MoveStep[]; order: string[]; changed: boolean };

/**
 * Plan a drop of `cardId` into `lane`, where `visibleIds` are the lane's
 * visible cards in their order after the drop (the dropped card included).
 * `order` is the full order of the lane to store (hidden cards keep their
 * place); `changed` is false when neither the lane nor the order changes.
 */
export function planDrop(cards: BoardCard[], cardId: string, lane: Lane, visibleIds: string[]): DropPlan | null {
	const card = cards.find((c) => c.card_id === cardId);
	if (!card) return null;
	const steps = planMove(card, lane);
	const laneBefore = groupByLane(cards, EMPTY_FILTERS)[lane].map((c) => c.card_id);
	const order = mergeVisibleOrder(
		laneBefore.filter((id) => id !== cardId),
		visibleIds
	);
	return { steps, order, changed: steps.length > 0 || order.join() !== laneBefore.join() };
}

/** The cards with `orderedIds` placed in `lane` at positions 0..n-1 (for an optimistic update). */
export function withLaneOrder<T extends BoardCard>(cards: T[], lane: Lane, orderedIds: string[]): T[] {
	const index = new Map(orderedIds.map((id, i) => [id, i]));
	return cards.map((c) => (index.has(c.card_id) ? { ...c, lane, position: index.get(c.card_id)! } : c));
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
export function applyMove<T extends ActionCard>(card: T, target: Lane, laneTagIds: LaneTagIds): T {
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
