// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Direct actions on My work rows (POST /claude-code/my-work/...). Pure
// helpers: which actions a row offers, labels, and how an action's result
// replaces the row in the loaded data. The row components only render these.

import type { MergeMethod, MyWork, WorkIssue, WorkPr } from './work';

export interface LinearState {
	id: string;
	name: string;
	type: string;
}

export type LinearAction = 'state' | 'assign_me' | 'priority' | 'comment';

export interface LinearActionResult {
	ok: boolean;
	issue: WorkIssue;
	removed: boolean;
	result?: { id: string | null; url: string | null };
}

export type PrAction = 'ready' | 'draft' | 'reviewers' | 'comment' | 'update_branch' | 'merge';

export interface PrActionRequest {
	repo: string;
	number: number;
	action: PrAction;
	reviewers?: string[];
	body?: string;
	method?: MergeMethod;
	head_sha?: string;
	delete_branch?: boolean;
}

export interface PrActionResult {
	ok: boolean;
	pr: WorkPr | null;
	removed: boolean;
	state: string | null;
	output?: string;
	note?: string;
}

/** The team key of an issue identifier (TRAD-12 → TRAD). */
export function teamOf(identifier: string): string {
	return identifier.split('-')[0] ?? identifier;
}

export const MERGE_METHOD_LABELS: Record<MergeMethod, string> = {
	squash: 'Squash and merge',
	merge: 'Create a merge commit',
	rebase: 'Rebase and merge'
};

export function shortSha(sha: string | undefined | null): string {
	return (sha ?? '').slice(0, 7);
}

/** The merge method preselected in the dialog: squash when allowed, else the first allowed one. */
export function defaultMergeMethod(pr: WorkPr): MergeMethod | null {
	const methods = pr.merge_methods ?? [];
	if (methods.includes('squash')) return 'squash';
	return methods[0] ?? null;
}

/** Whether the merge dialog may be opened, and why not. GitHub decides the rest. */
export function mergeAvailability(pr: WorkPr): { ok: boolean; reason: string | null } {
	if (pr.draft) return { ok: false, reason: 'Drafts cannot be merged' };
	if (!pr.head_sha) return { ok: false, reason: 'Head commit unknown; refresh first' };
	if (!(pr.merge_methods ?? []).length) return { ok: false, reason: 'The repository allows no merge method' };
	if (pr.conflicts || pr.merge_state === 'DIRTY') return { ok: false, reason: 'Resolve the conflicts first' };
	return { ok: true, reason: null };
}

/** GitHub reports the PR can be merged now (green, approved where required, up to date where required). */
export function readyToMerge(pr: WorkPr): boolean {
	return mergeAvailability(pr).ok && (pr.merge_state === 'CLEAN' || pr.merge_state === 'HAS_HOOKS');
}

const MERGE_STATE_LABELS: Record<string, string> = {
	CLEAN: 'Ready to merge',
	HAS_HOOKS: 'Ready to merge (hooks run on merge)',
	UNSTABLE: 'Mergeable, but some checks are not passing',
	BEHIND: 'Branch is behind the base branch',
	BLOCKED: 'Blocked by branch protection (review or checks)',
	DIRTY: 'Merge conflicts',
	DRAFT: 'Draft',
	UNKNOWN: 'GitHub has not computed the merge state yet'
};

export function mergeStateLabel(state: string | undefined): string {
	return MERGE_STATE_LABELS[state ?? 'UNKNOWN'] ?? state ?? 'Unknown';
}

/** The inline quick action of a PR row, if any. */
export function prQuickAction(pr: WorkPr): 'ready' | 'merge' | null {
	if (pr.draft) return 'ready';
	if (readyToMerge(pr)) return 'merge';
	return null;
}

/** Reviewer logins from free text: comma or space separated, a leading @ dropped, duplicates removed. */
export function parseReviewers(text: string): string[] {
	const out: string[] = [];
	for (const raw of text.split(/[\s,]+/)) {
		const login = raw.trim().replace(/^@/, '');
		if (login && !out.includes(login)) out.push(login);
	}
	return out;
}

/** The loaded data with one issue replaced (or dropped when it is no longer open). */
export function applyIssueResult(data: MyWork, identifier: string, res: LinearActionResult): MyWork {
	const issues = data.linear.issues;
	const next = res.removed
		? issues.filter((i) => i.identifier !== identifier)
		: issues.some((i) => i.identifier === identifier)
			? issues.map((i) => (i.identifier === identifier ? res.issue : i))
			: [...issues, res.issue];
	return { ...data, linear: { ...data.linear, issues: next } };
}

/** The loaded data with one pull request replaced (or dropped once merged or closed). */
export function applyPrResult(data: MyWork, repo: string, number: number, res: PrActionResult): MyWork {
	const same = (p: WorkPr) => p.repo === repo && p.number === number;
	let prs = data.github.prs;
	if (res.removed) prs = prs.filter((p) => !same(p));
	else if (res.pr) prs = prs.map((p) => (same(p) ? (res.pr as WorkPr) : p));
	return { ...data, github: { ...data.github, prs } };
}

/** A short past-tense line for a finished PR action. */
export function prDoneMessage(action: PrAction, res: PrActionResult): string {
	switch (action) {
		case 'ready':
			return 'Marked ready for review';
		case 'draft':
			return 'Converted to draft';
		case 'reviewers':
			return 'Reviewers requested';
		case 'comment':
			return 'Comment posted';
		case 'update_branch':
			return 'Branch updated';
		case 'merge':
			return res.state === 'MERGED' ? 'Merged' : 'Merge requested; GitHub has not merged it yet';
	}
}
