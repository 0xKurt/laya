// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure logic of the settings pickers (rule U1: no free-text fields for lists or
 * known values): checkbox lists, chip lists, the time zone select, effort
 * presets and folder lists.
 */

// ── checkbox lists ───────────────────────────────────────────────────────

/** The list with `item` added or removed, keeping the order of `all` when given. */
export function toggleItem(list: string[], item: string, all?: string[]): string[] {
	const next = list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
	if (!all) return next;
	const known = all.filter((x) => next.includes(x));
	return [...known, ...next.filter((x) => !all.includes(x))];
}

/** Every item checked, keeping checked values that are not offered (any more). */
export function selectAll(all: string[], selected: string[]): string[] {
	return [...all, ...selected.filter((s) => !all.includes(s))];
}

/** Checked values that are no longer offered (e.g. an org the login left). */
export function unknownSelected(all: string[], selected: string[]): string[] {
	return selected.filter((s) => !all.includes(s));
}

// ── chip lists ───────────────────────────────────────────────────────────

const EMAIL = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/;

/** "Ann <ANN@x.io>" / " ann@x.io " → "ann@x.io". */
export function normalizeEmail(input: string): string {
	const text = input.trim();
	const angled = /<([^>]+)>/.exec(text);
	return (angled ? angled[1] : text).trim().toLowerCase();
}

/** Why `value` cannot be added to the sender chips, or null. */
export function senderError(value: string, existing: string[]): string | null {
	if (!value) return 'Enter an email address.';
	if (!EMAIL.test(value)) return `"${value}" is not an email address.`;
	if (existing.map((e) => e.toLowerCase()).includes(value)) return `${value} is already skipped.`;
	return null;
}

/** The parts of typed or pasted text: "Ann <ann@x.io>, bob@x.io" → ["Ann <ann@x.io>", "bob@x.io"]. */
export function splitChipInput(input: string): string[] {
	const parts: string[] = [];
	for (const chunk of input.split(/[\n,;]+/)) {
		const text = chunk.trim();
		if (!text) continue;
		// "Name <address>" stays one part; otherwise every word is one.
		if (/<[^>]+>/.test(text)) parts.push(text);
		else parts.push(...text.split(/\s+/));
	}
	return parts;
}

/**
 * Add the typed text to the chips. Pasting several addresses at once (comma,
 * space or newline separated) adds each. Returns the new list, the first
 * problem, if any, and the parts that were not added (to keep in the input);
 * valid addresses are added even when another one fails.
 */
export function addChips(
	list: string[],
	input: string,
	normalize: (v: string) => string = normalizeEmail,
	validate: (v: string, existing: string[]) => string | null = senderError
): { list: string[]; error: string | null; rest: string } {
	const parts = splitChipInput(input);
	let next = [...list];
	let error: string | null = null;
	const bad: string[] = [];
	for (const part of parts.length ? parts : ['']) {
		const value = normalize(part);
		const problem = validate(value, next);
		if (problem) {
			error ??= problem;
			if (part) bad.push(part);
		} else next = [...next, value];
	}
	return { list: next, error, rest: bad.join(' ') };
}

export interface ChipSuggestion {
	value: string;
	label?: string;
	hint?: string;
}

export function removeChip(list: string[], value: string): string[] {
	return list.filter((v) => v !== value);
}

// ── time zones ───────────────────────────────────────────────────────────

/** Every IANA zone the runtime knows (falls back to a few if it knows none). */
export function allTimezones(): string[] {
	try {
		const zones = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone');
		if (zones && zones.length) return zones;
	} catch {
		// older runtime
	}
	return ['UTC', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'Asia/Tokyo'];
}

export function systemTimezone(): string {
	try {
		return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
	} catch {
		return 'UTC';
	}
}

/** Zones matching every word of the query ("new york", "berlin", "utc"), best first. */
export function filterTimezones(zones: string[], query: string, limit = 50): string[] {
	const words = query.trim().toLowerCase().replace(/_/g, ' ').split(/\s+/).filter(Boolean);
	if (!words.length) return zones.slice(0, limit);
	const scored: [number, string][] = [];
	for (const z of zones) {
		const hay = z.toLowerCase().replace(/_/g, ' ');
		if (!words.every((w) => hay.includes(w))) continue;
		const city = hay.split('/').pop() ?? hay;
		const score = city.startsWith(words[0]) ? 0 : hay.startsWith(words[0]) ? 1 : 2;
		scored.push([score, z]);
	}
	return scored.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1])).map(([, z]) => z).slice(0, limit);
}

/** "14:05" in `zone` at `now`; "" for an unknown zone. */
export function timeIn(zone: string, now: Date = new Date()): string {
	try {
		return new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit' }).format(now);
	} catch {
		return '';
	}
}

// ── effort presets ───────────────────────────────────────────────────────

export interface DurationPreset {
	label: string;
	minutes: number;
}

/** ½ day and 1 day are a working day of 8 hours. */
export const EFFORT_PRESETS: DurationPreset[] = [
	{ label: 'none', minutes: 0 },
	{ label: '15m', minutes: 15 },
	{ label: '30m', minutes: 30 },
	{ label: '1h', minutes: 60 },
	{ label: '2h', minutes: 120 },
	{ label: '½ day', minutes: 240 },
	{ label: '1 day', minutes: 480 }
];

/** The preset matching `minutes`, or null (then the value is a custom one). */
export function presetFor(minutes: number | null | undefined, presets = EFFORT_PRESETS): DurationPreset | null {
	if (minutes == null) return null;
	return presets.find((p) => p.minutes === minutes) ?? null;
}

// ── folder lists ─────────────────────────────────────────────────────────

/** Adds `path` (trailing slashes trimmed) unless it is already listed. */
export function addFolder(list: string[], path: string): string[] {
	const p = path.trim().replace(/(.)\/+$/, '$1');
	if (!p || list.includes(p)) return list;
	return [...list, p];
}

// ── duration selects (settings intervals) ────────────────────────────────

export type DurationUnit = 'minutes' | 'hours' | 'days';

/** 90 minutes → "1 h 30 min", 48 hours → "2 days", 1 day → "1 day". */
export function durationLabel(value: number, unit: DurationUnit): string {
	const minutes = unit === 'minutes' ? value : unit === 'hours' ? value * 60 : value * 1440;
	if (minutes === 0) return unit === 'minutes' ? '0 min' : unit === 'hours' ? 'right away' : '0 days';
	const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;
	if (minutes % 10080 === 0) return plural(minutes / 10080, 'week');
	if (minutes % 1440 === 0) return plural(minutes / 1440, 'day');
	if (minutes % 60 === 0) return plural(minutes / 60, 'hour');
	if (minutes > 60) return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
	return `${minutes} min`;
}

/** The preset values plus the current one when it is not a preset, ascending. */
export function durationOptions(presets: number[], current: number | null | undefined): number[] {
	const all = new Set(presets);
	if (typeof current === 'number' && Number.isFinite(current)) all.add(current);
	return [...all].sort((a, b) => a - b);
}
