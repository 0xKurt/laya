// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// The app's top-level views and the pure rules for hiding them. The hidden
// list is stored in engine settings as `views.hidden`; at least one view
// always stays visible.

export type ViewId = 'pulse' | 'board' | 'work' | 'claude' | 'omni' | 'coherence' | 'corpus';

export interface ViewItem {
	id: ViewId;
	label: string;
	href: string;
}

/** All views in navigation order. */
export const VIEWS: readonly ViewItem[] = [
	{ id: 'pulse', label: 'Pulse', href: '/feed' },
	{ id: 'board', label: 'Board', href: '/board' },
	{ id: 'work', label: 'My work', href: '/work' },
	{ id: 'claude', label: 'Assistant', href: '/terminal' },
	{ id: 'omni', label: 'Omni', href: '/omni' },
	{ id: 'coherence', label: 'Coherence', href: '/coherence' },
	{ id: 'corpus', label: 'Corpus', href: '/corpus' }
];

/** Order in which the landing page picks the first visible view. */
export const LANDING_ORDER: readonly ViewId[] = ['board', 'work', 'claude', 'pulse', 'omni', 'coherence', 'corpus'];

const IDS = new Set<string>(VIEWS.map((v) => v.id));

/**
 * Turn a stored value into a valid hidden list: unknown ids and duplicates are
 * dropped, and a list that would hide every view is treated as hiding nothing.
 */
export function normalizeHidden(raw: unknown): ViewId[] {
	if (!Array.isArray(raw)) return [];
	const out: ViewId[] = [];
	for (const id of raw) {
		if (typeof id === 'string' && IDS.has(id) && !out.includes(id as ViewId)) out.push(id as ViewId);
	}
	return out.length >= VIEWS.length ? [] : out;
}

/** Views to show in the navigation, in navigation order. */
export function visibleViews(hidden: readonly ViewId[]): ViewItem[] {
	return VIEWS.filter((v) => !hidden.includes(v.id));
}

/** Path of the first visible view in landing order (Board when nothing is hidden). */
export function landingPath(hidden: readonly ViewId[]): string {
	const id = LANDING_ORDER.find((v) => !hidden.includes(v)) ?? 'board';
	return VIEWS.find((v) => v.id === id)!.href;
}

/** Whether the view at `href` is hidden. */
export function isHrefHidden(hidden: readonly ViewId[], href: string): boolean {
	const view = VIEWS.find((v) => v.href === href);
	return view ? hidden.includes(view.id) : false;
}

/** Whether `id` may be hidden: false when it is the last visible view. */
export function canHide(hidden: readonly ViewId[], id: ViewId): boolean {
	return hidden.includes(id) || visibleViews(hidden).length > 1;
}

/**
 * The hidden list after showing or hiding `id`. Hiding the last visible view
 * returns the list unchanged.
 */
export function setVisible(hidden: readonly ViewId[], id: ViewId, visible: boolean): ViewId[] {
	if (visible) return hidden.filter((v) => v !== id);
	if (!canHide(hidden, id) || hidden.includes(id)) return [...hidden];
	return VIEWS.map((v) => v.id).filter((v) => v === id || hidden.includes(v));
}
