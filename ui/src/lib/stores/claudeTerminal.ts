// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * The Assistant view's active tab, and "Send to Assistant", which goes to the
 * docked assistant session in the side panel.
 *
 * The sessions themselves live in the engine; this store only remembers the
 * most recently active tab (also across reloads, in localStorage).
 */

import { get, writable } from 'svelte/store';
import { engineApi } from '$lib/api/engine';
import { routeReference } from '$lib/claude/assistantReference';
import { sidePanel } from '$lib/stores/sidePanel';

const ACTIVE_KEY = 'laya.terminal.activeId';

function storedActive(): string | null {
	try {
		return typeof localStorage === 'undefined' ? null : localStorage.getItem(ACTIVE_KEY);
	} catch {
		return null;
	}
}

/** Id of the most recently active terminal tab. */
export const activeTerminalId = writable<string | null>(storedActive());

activeTerminalId.subscribe((id) => {
	try {
		if (typeof localStorage === 'undefined') return;
		if (id) localStorage.setItem(ACTIVE_KEY, id);
		else localStorage.removeItem(ACTIVE_KEY);
	} catch {
		// Storage can be unavailable (private mode); the store still works in memory.
	}
});

/** Shows a short toast without depending on the page that is open. */
export function showClaudeToast(message: string, variant: 'info' | 'warning' = 'info'): void {
	if (typeof document === 'undefined') return;
	const el = document.createElement('div');
	el.setAttribute('role', 'status');
	el.className =
		'fixed bottom-4 left-1/2 z-[100] -translate-x-1/2 rounded-lg border px-4 py-2 text-sm shadow-lg backdrop-blur-sm ' +
		(variant === 'warning'
			? 'border-amber-700/50 bg-amber-950/90 text-amber-200'
			: 'border-surface-600/50 bg-surface-800/90 text-surface-200');
	el.textContent = message;
	document.body.appendChild(el);
	setTimeout(() => el.remove(), 2500);
}

/** The docked assistant's terminal once it is attached and at its prompt (session id). */
export const assistantReady = writable<string | null>(null);

/** A reference waiting for the docked assistant to become ready (inserted once). */
export const assistantPendingReference = writable<string | null>(null);

/** Bumped to ask the docked assistant's terminal to take focus. */
export const assistantFocusRequest = writable(0);

/**
 * Inserts `text` into the docked assistant session: opens the side panel when
 * it is closed, inserts once (no Enter) and focuses the terminal, or queues the
 * text while the session is starting. See `lib/claude/assistantReference.ts`.
 */
export async function referenceInClaude(text: string): Promise<void> {
	try {
		const outcome = await routeReference(text, {
			state: () => get(sidePanel),
			running: async () => (await engineApi.listClaudeTerminals()).sessions.filter((s) => s.running).map((s) => s.id),
			openPanel: () => sidePanel.openAssistant(),
			readyId: () => get(assistantReady),
			send: (id, t) => engineApi.sendClaudeTerminalInput(id, t).then(() => undefined),
			focus: () => assistantFocusRequest.update((n) => n + 1),
			queue: (t) => assistantPendingReference.set(t)
		});
		if (outcome !== 'queued') showClaudeToast('Sent to Assistant');
	} catch (e) {
		showClaudeToast(`Could not send to Assistant: ${e instanceof Error ? e.message : String(e)}`, 'warning');
	}
}
