// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';
import {
	CHAT_WIDTH,
	PANEL_DEFAULT,
	PANEL_MAX,
	PANEL_MIN,
	STORAGE_KEY,
	clampWidth,
	createSidePanel,
	loadState
} from './sidePanel';
import { routeReference, takeQueued, type ReferenceDeps } from '$lib/claude/assistantReference';

function memoryStorage(initial: Record<string, string> = {}) {
	const data = { ...initial };
	return {
		data,
		getItem: (k: string) => (k in data ? data[k] : null),
		setItem: (k: string, v: string) => {
			data[k] = v;
		}
	};
}

function fakeRoot() {
	const props: Record<string, string> = {};
	return { props, style: { setProperty: (k: string, v: string) => (props[k] = v) } };
}

describe('side panel store', () => {
	it('clamps the width to 440–800 and defaults to 520', () => {
		expect(clampWidth(100)).toBe(PANEL_MIN);
		expect(clampWidth(5000)).toBe(PANEL_MAX);
		expect(clampWidth(612.4)).toBe(612);
		expect(clampWidth('x')).toBe(PANEL_DEFAULT);
		expect(clampWidth(undefined)).toBe(PANEL_DEFAULT);
	});

	it('persists panel, width and session id, and restores them', () => {
		const storage = memoryStorage();
		const chat = writable(false);
		const panel = createSidePanel(chat, { storage, root: null });
		panel.openAssistant();
		panel.setWidth(700);
		panel.setSession('abc');
		const restored = loadState(storage);
		expect(restored).toEqual({ panel: 'assistant', width: 700, sessionId: 'abc', host: 'panel' });
	});

	it('survives broken or unavailable storage', () => {
		expect(loadState(memoryStorage({ [STORAGE_KEY]: '{not json' }))).toMatchObject({ panel: null, width: PANEL_DEFAULT });
		expect(loadState(memoryStorage({ [STORAGE_KEY]: '{"width": 9999, "panel": "chat"}' }))).toMatchObject({
			panel: null,
			width: PANEL_MAX
		});
		const throwing = {
			getItem: () => {
				throw new Error('denied');
			},
			setItem: () => {
				throw new Error('denied');
			}
		};
		expect(loadState(throwing).width).toBe(PANEL_DEFAULT);
		const panel = createSidePanel(writable(false), { storage: throwing, root: null });
		expect(() => panel.openAssistant()).not.toThrow();
	});

	it('is mutually exclusive with Laya chat', () => {
		const chat = writable(false);
		const panel = createSidePanel(chat, { storage: memoryStorage(), root: null });
		panel.openAssistant();
		chat.set(true);
		expect(get(panel).panel).toBe('chat');
		panel.openAssistant();
		expect(get(chat)).toBe(false);
		expect(get(panel).panel).toBe('assistant');
		chat.set(true);
		chat.set(false);
		expect(get(panel).panel).toBe(null);
		panel.toggleAssistant();
		expect(get(panel).panel).toBe('assistant');
		panel.toggleAssistant();
		expect(get(panel).panel).toBe(null);
	});

	it('sets --side-panel-w to the slot width', () => {
		const root = fakeRoot();
		const chat = writable(false);
		const panel = createSidePanel(chat, { storage: memoryStorage(), root });
		expect(root.props['--side-panel-w']).toBe('0px');
		panel.openAssistant();
		panel.setWidth(600);
		expect(root.props['--side-panel-w']).toBe('600px');
		chat.set(true);
		expect(root.props['--side-panel-w']).toBe(`${CHAT_WIDTH}px`);
	});

	it('hands the docked session to the Claude view and back', () => {
		const panel = createSidePanel(writable(false), { storage: memoryStorage(), root: null });
		panel.openAssistant();
		panel.setHost('view');
		expect(get(panel)).toMatchObject({ host: 'view', panel: null });
		panel.openAssistant();
		expect(get(panel)).toMatchObject({ host: 'panel', panel: 'assistant' });
	});
});

describe('reference routing', () => {
	function deps(over: Partial<ReferenceDeps> = {}) {
		const calls = { open: 0, focus: 0, sent: [] as Array<[string, string]>, queued: [] as string[] };
		const d: ReferenceDeps = {
			state: () => ({ host: 'panel', sessionId: 's1' }),
			running: async () => ['s1'],
			openPanel: () => void calls.open++,
			readyId: () => null,
			send: async (id, text) => void calls.sent.push([id, text]),
			focus: () => void calls.focus++,
			queue: (t) => void calls.queued.push(t),
			...over
		};
		return { d, calls };
	}

	it('opens the closed panel and queues while the session is not ready', async () => {
		const { d, calls } = deps();
		expect(await routeReference('[TRAD-1] x', d)).toBe('queued');
		expect(calls).toMatchObject({ open: 1, sent: [], queued: ['[TRAD-1] x'] });
	});

	it('inserts once and focuses when the panel is ready', async () => {
		const { d, calls } = deps({ readyId: () => 's1' });
		expect(await routeReference('ref', d)).toBe('sent-panel');
		expect(calls).toMatchObject({ open: 1, focus: 1, sent: [['s1', 'ref']], queued: [] });
	});

	it('goes to the Claude view when the docked session is shown there', async () => {
		const { d, calls } = deps({ state: () => ({ host: 'view', sessionId: 's1' }) });
		expect(await routeReference('ref', d)).toBe('sent-view');
		expect(calls).toMatchObject({ open: 0, sent: [['s1', 'ref']] });
	});

	it('falls back to the panel when the view-hosted session is gone', async () => {
		const { d, calls } = deps({ state: () => ({ host: 'view', sessionId: 's1' }), running: async () => [] });
		expect(await routeReference('ref', d)).toBe('queued');
		expect(calls.open).toBe(1);
	});

	it('takes a queued reference exactly once', () => {
		let pending: string | null = 'ref';
		const clear = vi.fn(() => (pending = null));
		expect(takeQueued(() => pending, clear)).toBe('ref');
		expect(takeQueued(() => pending, clear)).toBe(null);
		expect(clear).toHaveBeenCalledTimes(1);
	});
});
