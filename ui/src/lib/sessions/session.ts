// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Pure logic of the effort estimate and the session lane
// (docs/claude-code-connector.md, "Effort and session lane"): formatting and
// parsing durations, the per-lane sum of the user's time, and the wording of
// a session's claims after the engine checked them with gh.

import type { ActionCard, ClaimKind, Effort, SessionClaim } from '$lib/api/types';

/** 15 → "15m", 120 → "2h", 90 → "1h30m". */
export function formatMinutes(minutes: number): string {
	const m = Math.max(0, Math.round(minutes));
	if (m < 60) return `${m}m`;
	const h = Math.floor(m / 60);
	const rest = m % 60;
	return rest ? `${h}h${rest}m` : `${h}h`;
}

/** The card's effort line, e.g. "you ~30m · agent ~2h". */
export function formatEffort(effort: Effort): string {
	return `you ~${formatMinutes(effort.you_minutes)} · agent ~${formatMinutes(effort.agent_minutes)}`;
}

/** The effort of a card: the copy on the card, else the one inside staged_output. */
export function effortOf(card: ActionCard): Effort | null {
	return card.effort ?? card.staged_output?.effort ?? null;
}

/**
 * Parse a duration typed by the user: "45", "45m", "2h", "1h30m", "1.5h".
 * A bare number is minutes. Returns null for anything else.
 */
export function parseDuration(input: string): number | null {
	const text = input.trim().toLowerCase().replace(/\s+/g, '');
	if (!text) return null;
	if (/^\d+$/.test(text)) return Number(text);
	const m = /^(?:(\d+(?:\.\d+)?)h)?(?:(\d+)m(?:in)?)?$/.exec(text);
	if (!m || (!m[1] && !m[2])) return null;
	return Math.round(Number(m[1] ?? 0) * 60 + Number(m[2] ?? 0));
}

/** Sum of the user's minutes over the cards that have an estimate. */
export function sumYouMinutes(cards: ActionCard[]): number {
	return cards.reduce((sum, c) => sum + (effortOf(c)?.you_minutes ?? 0), 0);
}

const CLAIM_WORDS: Record<ClaimKind, string> = { merged: 'merged', green: 'checks green', deployed: 'deployed' };

/** Quick client-side check whether a text claims merged / green / deployed. */
export function mentionsClaim(text: string | null | undefined): boolean {
	return !!text && /\b(merged|green|deployed|checks? (are |is )?pass(ed|ing)|ci (is )?pass(ed|ing))\b/i.test(text);
}

/** The card text the engine scans for claims. */
export function cardClaimText(card: ActionCard): string {
	return [card.header, card.summary, ...(card.intelligence ?? []), card.staged_output?.content ?? ''].join('\n');
}

export type ClaimLine = { text: string; tone: 'fact' | 'conflict' | 'lead' };

/**
 * One line per claim. Only a claim gh confirmed reads as a fact; everything
 * else says it came from the session.
 */
export function claimLines(claim: SessionClaim): ClaimLine[] {
	const pr = claim.repo && claim.number != null ? `${claim.repo}#${claim.number}` : '';
	return claim.claims.map((kind) => {
		const word = CLAIM_WORDS[kind];
		const subject = pr ? `${pr} ${word}` : word;
		const result = claim.results[kind] ?? 'unverified';
		if (result === 'confirmed') return { text: `${subject} (checked with gh)`, tone: 'fact' };
		if (result === 'contradicted') {
			const actual =
				kind === 'merged' ? (claim.state ?? 'unknown').toLowerCase() : `checks ${claim.checks ?? 'unknown'}`;
			return { text: `${pr || 'PR'}: session says ${word}, gh says ${actual}`, tone: 'conflict' };
		}
		return { text: `${subject}: reported by the session, unverified`, tone: 'lead' };
	});
}
