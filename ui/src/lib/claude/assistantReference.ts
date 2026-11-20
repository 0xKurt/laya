// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * "Send to Assistant" routing to the docked assistant session.
 *
 * - The docked session is drawn in the Assistant view ("Open here") and running:
 *   insert there directly, the panel stays as it is.
 * - Otherwise open the panel. When the panel's session is ready (attached and
 *   at its prompt), insert and focus the terminal; while it is starting, queue
 *   the reference: the panel inserts it once when it becomes ready.
 *
 * Inserting never presses Enter (the engine sends one bracketed paste).
 */

export interface ReferenceDeps {
	/** Where the docked session is drawn and its id. */
	state(): { host: 'panel' | 'view'; sessionId: string | null };
	/** Ids of the running terminal sessions. */
	running(): Promise<string[]>;
	openPanel(): void;
	/** Session id of the panel's terminal once it is ready for input, else null. */
	readyId(): string | null;
	send(id: string, text: string): Promise<void>;
	focus(): void;
	queue(text: string): void;
}

export type ReferenceOutcome = 'sent-view' | 'sent-panel' | 'queued';

export async function routeReference(text: string, deps: ReferenceDeps): Promise<ReferenceOutcome> {
	const { host, sessionId } = deps.state();
	if (host === 'view' && sessionId && (await deps.running()).includes(sessionId)) {
		await deps.send(sessionId, text);
		return 'sent-view';
	}
	deps.openPanel();
	const ready = deps.readyId();
	if (ready) {
		await deps.send(ready, text);
		deps.focus();
		return 'sent-panel';
	}
	deps.queue(text);
	return 'queued';
}

/** Takes the queued reference exactly once (null when there is none). */
export function takeQueued(get: () => string | null, clear: () => void): string | null {
	const text = get();
	if (text) clear();
	return text;
}
