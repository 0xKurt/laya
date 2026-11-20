// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * One-line references the "Send to Assistant" button inserts into an assistant
 * terminal tab. They are built only from fields the UI already has; the
 * engine strips control characters again before pasting.
 */

import type { ActionCard } from '$lib/api/types';
import type { WorkIssue, WorkPr } from '$lib/work/work';
import { platformKey } from '$lib/utils/cardVisuals';

/** Collapses whitespace (line breaks included) so a reference stays on one line. */
export function oneLine(text: string | null | undefined): string {
	return (text ?? '').replace(/\s+/g, ' ').trim();
}

function withUrl(head: string, url: string | null | undefined): string {
	const link = oneLine(url);
	return link ? `${head} — ${link}` : head;
}

/** `[<platform> card] <header> — <source_url>` */
export function cardReference(card: Pick<ActionCard, 'header' | 'source_url' | 'entity_id'>): string {
	const platform = platformKey(card.entity_id) || 'Laya';
	return withUrl(`[${platform} card] ${oneLine(card.header)}`, card.source_url);
}

/** `[<identifier>] <title> — <url>` */
export function linearReference(issue: Pick<WorkIssue, 'identifier' | 'title' | 'url'>): string {
	return withUrl(`[${oneLine(issue.identifier)}] ${oneLine(issue.title)}`, issue.url);
}

/** `[<repo>#<number>] <title> — <url>` */
export function prReference(pr: Pick<WorkPr, 'repo' | 'number' | 'title' | 'url'>): string {
	return withUrl(`[${oneLine(pr.repo)}#${pr.number}] ${oneLine(pr.title)}`, pr.url);
}
