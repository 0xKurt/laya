// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * The right-hand side panel slot: the docked assistant (a terminal session
 * running the coding agent) or Laya's Chat, never both.
 *
 * - `panel`: what the slot shows. `'chat'` mirrors the chat store's `chatOpen`
 *   (Laya Chat keeps its own open state); opening one closes the other.
 * - `width`: the assistant panel's width (440–800 px, default 520).
 * - `sessionId`: the docked assistant's terminal session (Laya id).
 * - `host`: where the docked session is drawn. One session renders in one view
 *   only (two xterm views would fight over the PTY size), so the Assistant view
 *   shows it only after "Open here" (`'view'`), and opening the panel takes it
 *   back (`'panel'`).
 *
 * Panel (assistant or closed), width and session id persist in localStorage.
 * `--side-panel-w` on the document root is the slot's current width (0px when
 * closed), so the main area can pad for it.
 */

import { get, writable, type Readable, type Writable } from 'svelte/store';
import { chatOpen } from './chat';

export type SidePanel = 'assistant' | 'chat' | null;
export type AssistantHost = 'panel' | 'view';

export interface SidePanelState {
	panel: SidePanel;
	width: number;
	sessionId: string | null;
	host: AssistantHost;
}

export const PANEL_MIN = 440;
export const PANEL_MAX = 800;
export const PANEL_DEFAULT = 520;
/** Laya Chat's sidebar width (ChatSidebar.svelte). */
export const CHAT_WIDTH = 460;
export const STORAGE_KEY = 'laya.sidePanel';

export function clampWidth(width: unknown): number {
	const n = typeof width === 'number' ? width : Number(width);
	if (!Number.isFinite(n)) return PANEL_DEFAULT;
	return Math.round(Math.min(PANEL_MAX, Math.max(PANEL_MIN, n)));
}

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;

function storage(): StorageLike | null {
	try {
		return typeof localStorage === 'undefined' ? null : localStorage;
	} catch {
		return null;
	}
}

export function loadState(store: StorageLike | null = storage()): SidePanelState {
	const fallback: SidePanelState = { panel: null, width: PANEL_DEFAULT, sessionId: null, host: 'panel' };
	try {
		const raw = store?.getItem(STORAGE_KEY);
		if (!raw) return fallback;
		const parsed = JSON.parse(raw) as Partial<SidePanelState>;
		return {
			// Laya Chat's open state is not persisted (upstream behaviour).
			panel: parsed.panel === 'assistant' ? 'assistant' : null,
			width: clampWidth(parsed.width),
			sessionId: typeof parsed.sessionId === 'string' && parsed.sessionId ? parsed.sessionId : null,
			host: 'panel'
		};
	} catch {
		return fallback;
	}
}

export function saveState(state: SidePanelState, store: StorageLike | null = storage()): void {
	try {
		store?.setItem(
			STORAGE_KEY,
			JSON.stringify({ panel: state.panel === 'assistant' ? 'assistant' : null, width: state.width, sessionId: state.sessionId })
		);
	} catch {
		// Storage can be unavailable (private mode); the panel still works in memory.
	}
}

/** Width the main area pads for: the slot's width, 0 when closed. */
export function slotWidth(state: SidePanelState): number {
	if (state.panel === 'assistant') return state.width;
	if (state.panel === 'chat') return CHAT_WIDTH;
	return 0;
}

export interface SidePanelStore extends Readable<SidePanelState> {
	openAssistant(): void;
	toggleAssistant(): void;
	close(): void;
	setWidth(width: number): void;
	setSession(id: string | null): void;
	setHost(host: AssistantHost): void;
}

/**
 * Creates the store and keeps it mutually exclusive with `chatOpen`: opening
 * the assistant closes Laya Chat; Laya Chat opening takes the slot.
 */
export function createSidePanel(
	chatOpen: Writable<boolean>,
	opts: { storage?: StorageLike | null; root?: { style: { setProperty(k: string, v: string): void } } | null } = {}
): SidePanelStore {
	const store = opts.storage === undefined ? storage() : opts.storage;
	const initial = loadState(store);
	if (initial.panel === 'assistant' && get(chatOpen)) initial.panel = 'chat';
	const state = writable<SidePanelState>(initial);

	const root = opts.root === undefined ? (typeof document === 'undefined' ? null : document.documentElement) : opts.root;
	state.subscribe((s) => {
		saveState(s, store);
		root?.style.setProperty('--side-panel-w', `${slotWidth(s)}px`);
	});

	chatOpen.subscribe((open) => {
		const s = get(state);
		if (open && s.panel !== 'chat') state.set({ ...s, panel: 'chat' });
		else if (!open && s.panel === 'chat') state.set({ ...s, panel: null });
	});

	function openAssistant() {
		if (get(chatOpen)) chatOpen.set(false);
		state.update((s) => ({ ...s, panel: 'assistant', host: 'panel' }));
	}

	return {
		subscribe: state.subscribe,
		openAssistant,
		toggleAssistant() {
			if (get(state).panel === 'assistant') state.update((s) => ({ ...s, panel: null }));
			else openAssistant();
		},
		close() {
			if (get(chatOpen)) chatOpen.set(false);
			state.update((s) => ({ ...s, panel: null }));
		},
		setWidth(width: number) {
			state.update((s) => ({ ...s, width: clampWidth(width) }));
		},
		setSession(id: string | null) {
			state.update((s) => ({ ...s, sessionId: id }));
		},
		setHost(host: AssistantHost) {
			state.update((s) => ({ ...s, host, panel: host === 'view' && s.panel === 'assistant' ? null : s.panel }));
		}
	};
}


/** The app's side panel slot. */
export const sidePanel = createSidePanel(chatOpen);
