// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * Which Claude terminal tab receives "Reference in Claude", and the hand-off
 * of a reference to the terminal page when no tab is open yet.
 *
 * The sessions themselves live in the engine; this store only remembers the
 * most recently active tab (also across reloads, in localStorage).
 */

import { get, writable } from 'svelte/store';
import { goto } from '$app/navigation';
import { engineApi } from '$lib/api/engine';

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

/**
 * A reference waiting for a terminal: set when no tab is open, consumed by the
 * terminal page once the new tab's process has started.
 */
export const pendingReference = writable<string | null>(null);

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

/**
 * Inserts `text` into the most recently active running terminal tab, or opens
 * `/terminal` with a new tab and inserts it there once claude has started.
 */
export async function referenceInClaude(text: string): Promise<void> {
	try {
		const { sessions } = await engineApi.listClaudeTerminals();
		const running = sessions.filter((s) => s.running);
		const activeId = get(activeTerminalId);
		const target = running.find((s) => s.id === activeId) ?? running[running.length - 1];
		if (target) {
			await engineApi.sendClaudeTerminalInput(target.id, text);
			activeTerminalId.set(target.id);
			showClaudeToast('Referenced in Claude');
			return;
		}
		pendingReference.set(text);
		await goto('/terminal');
	} catch (e) {
		showClaudeToast(`Could not reference in Claude: ${e instanceof Error ? e.message : String(e)}`, 'warning');
	}
}
