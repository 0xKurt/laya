// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// "Sync now" on the Board: one sweep of every enabled source plus a re-check
// of open cards (POST /claude-code/sync-all), and the line next to the button
// that says when the sources last synced and sync next.

/** GET/POST /claude-code/sync-all. */
export type SyncAllStatus = {
	running: boolean;
	started_at: string | null;
	finished_at: string | null;
	/** Per platform, once its sweep finished. */
	results: Record<string, { events: number; error: string | null }>;
	/** The enabled sources a sync covers. */
	platforms: string[];
	/** The latest successful sweep of any enabled source. */
	last_ok_at: string | null;
	/** The next scheduled sweep of any enabled source. */
	next_at: string | null;
	budget_paused: boolean;
};

function parse(iso: string | null): Date | null {
	if (!iso) return null;
	const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
	return Number.isNaN(d.getTime()) ? null : d;
}

function ago(d: Date, now: Date): string {
	const mins = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 60000));
	if (mins < 1) return 'just now';
	if (mins < 60) return `${mins} min ago`;
	const hours = Math.floor(mins / 60);
	if (hours < 24) return `${hours} h ago`;
	return `${Math.floor(hours / 24)} d ago`;
}

function clock(d: Date, now: Date): string {
	const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
	if (d.toDateString() === now.toDateString()) return hm;
	return `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}`;
}

/** "Last synced 12 min ago · next ~14:05"; empty when no source is enabled. */
export function syncLabel(status: SyncAllStatus | null, now: Date = new Date()): string {
	if (!status || status.platforms.length === 0) return '';
	const last = parse(status.last_ok_at);
	const next = parse(status.next_at);
	const parts = [last ? `Last synced ${ago(last, now)}` : 'Not synced yet'];
	if (next) parts.push(`next ~${clock(next, now)}`);
	return parts.join(' · ');
}

/** "3 of 5 sources" while a sync runs. */
export function syncProgress(status: SyncAllStatus): string {
	return `${Object.keys(status.results).length} of ${status.platforms.length} sources`;
}

/** The sources whose sweep failed in the last sync, as "Slack: reason" lines. */
export function syncErrors(status: SyncAllStatus | null, labels: Record<string, string> = {}): string[] {
	if (!status) return [];
	return Object.entries(status.results)
		.filter(([, r]) => r.error)
		.map(([p, r]) => `${labels[p] ?? p}: ${r.error}`);
}
