// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// The app's top-level views, the panels that can be hidden like them, and
// the pure rules for hiding them. The hidden list is stored in engine
// settings as `views.hidden`; at least one view always stays visible (a
// panel never counts toward that).

export type ViewId = 'pulse' | 'board' | 'work' | 'claude' | 'omni' | 'coherence' | 'corpus';

/** Panels outside the view navigation that can be hidden too. */
export type PanelId = 'chat';

export type HideableId = ViewId | PanelId;

export interface PanelItem {
	id: PanelId;
	label: string;
	hint: string;
}

/** Hiding a panel removes its titlebar button and keyboard shortcut; nothing is deleted. */
export const PANELS: readonly PanelItem[] = [
	{ id: 'chat', label: 'Chat', hint: 'The Assistant can do everything Chat does and more.' }
];

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

const VIEW_IDS = new Set<string>(VIEWS.map((v) => v.id));
const PANEL_IDS = new Set<string>(PANELS.map((p) => p.id));
const ORDER: readonly HideableId[] = [...VIEWS.map((v) => v.id), ...PANELS.map((p) => p.id)];

function isPanel(id: HideableId): id is PanelId {
	return PANEL_IDS.has(id);
}

/**
 * Turn a stored value into a valid hidden list: unknown ids and duplicates are
 * dropped, and a list that would hide every view shows all views again
 * (hidden panels stay hidden).
 */
export function normalizeHidden(raw: unknown): HideableId[] {
	if (!Array.isArray(raw)) return [];
	const out: HideableId[] = [];
	for (const id of raw) {
		if (typeof id === 'string' && (VIEW_IDS.has(id) || PANEL_IDS.has(id)) && !out.includes(id as HideableId)) {
			out.push(id as HideableId);
		}
	}
	const views = out.filter((id) => VIEW_IDS.has(id));
	return views.length >= VIEWS.length ? out.filter((id) => PANEL_IDS.has(id)) : out;
}

/** Whether a panel (e.g. Chat) is hidden. */
export function isPanelHidden(hidden: readonly HideableId[], id: PanelId): boolean {
	return hidden.includes(id);
}

/** Views to show in the navigation, in navigation order. */
export function visibleViews(hidden: readonly HideableId[]): ViewItem[] {
	return VIEWS.filter((v) => !hidden.includes(v.id));
}

/** Path of the first visible view in landing order (Board when nothing is hidden). */
export function landingPath(hidden: readonly HideableId[]): string {
	const id = LANDING_ORDER.find((v) => !hidden.includes(v)) ?? 'board';
	return VIEWS.find((v) => v.id === id)!.href;
}

/** Whether the view at `href` is hidden. */
export function isHrefHidden(hidden: readonly HideableId[], href: string): boolean {
	const view = VIEWS.find((v) => v.href === href);
	return view ? hidden.includes(view.id) : false;
}

/** Whether `id` may be hidden: false when it is the last visible view. Panels always may. */
export function canHide(hidden: readonly HideableId[], id: HideableId): boolean {
	return isPanel(id) || hidden.includes(id) || visibleViews(hidden).length > 1;
}

/**
 * The hidden list after showing or hiding `id`, views first then panels, each
 * in their order. Hiding the last visible view returns the list unchanged.
 */
export function setVisible(hidden: readonly HideableId[], id: HideableId, visible: boolean): HideableId[] {
	if (visible) return hidden.filter((v) => v !== id);
	if (!canHide(hidden, id) || hidden.includes(id)) return [...hidden];
	return ORDER.filter((v) => v === id || hidden.includes(v));
}
