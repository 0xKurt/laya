// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	applyIssueResult,
	applyPrResult,
	defaultMergeMethod,
	mergeAvailability,
	mergeStateLabel,
	parseReviewers,
	prDoneMessage,
	prQuickAction,
	readyToMerge,
	shortSha,
	teamOf
} from './actions';
import type { MyWork, WorkIssue, WorkPr } from './work';

const SHA = 'abcdef1234567890abcdef1234567890abcdef12';

function pr(over: Partial<WorkPr> = {}): WorkPr {
	return {
		repo: 'acme/app',
		number: 1,
		title: 'PR',
		url: 'https://github.com/acme/app/pull/1',
		draft: false,
		created_at: null,
		updated_at: null,
		base: 'main',
		branch: 'kurt/x',
		mergeable: 'MERGEABLE',
		conflicts: false,
		review_decision: 'APPROVED',
		requested_reviewers: 0,
		reviews: [],
		approvals: 1,
		changes_requested: 0,
		unresolved_threads: 0,
		checks: { state: 'success', failing: 0, pending: 0, total: 2 },
		linear_key: null,
		author: 'kurt',
		head_sha: SHA,
		merge_state: 'CLEAN',
		merge_methods: ['squash', 'rebase'],
		delete_branch_on_merge: true,
		...over
	};
}

function issue(identifier: string, over: Partial<WorkIssue> = {}): WorkIssue {
	return {
		identifier,
		title: identifier,
		url: null,
		state: 'Todo',
		state_type: 'unstarted',
		priority: 3,
		project: null,
		cycle: null,
		due_date: null,
		updated_at: null,
		labels: [],
		assignee: null,
		assigned_to_me: true,
		created_by_me: false,
		...over
	};
}

function work(issues: WorkIssue[], prs: WorkPr[]): MyWork {
	const section = { connected: true, error: null, note: null, fetched_at: null, attempted_at: null, cached: false, refreshing: false };
	return { github: { ...section, prs }, linear: { ...section, issues } };
}

describe('pull request actions', () => {
	it('offers ready for review on drafts and merge only when GitHub says clean', () => {
		expect(prQuickAction(pr({ draft: true, merge_state: 'DRAFT' }))).toBe('ready');
		expect(prQuickAction(pr())).toBe('merge');
		expect(prQuickAction(pr({ merge_state: 'HAS_HOOKS' }))).toBe('merge');
		expect(prQuickAction(pr({ merge_state: 'BLOCKED' }))).toBeNull();
		expect(prQuickAction(pr({ merge_state: 'BEHIND' }))).toBeNull();
		expect(readyToMerge(pr({ merge_methods: [] }))).toBe(false);
	});

	it('explains why merge is unavailable', () => {
		expect(mergeAvailability(pr())).toEqual({ ok: true, reason: null });
		expect(mergeAvailability(pr({ draft: true })).reason).toMatch(/Drafts/);
		expect(mergeAvailability(pr({ head_sha: '' })).reason).toMatch(/Head commit/);
		expect(mergeAvailability(pr({ merge_methods: [] })).reason).toMatch(/no merge method/);
		expect(mergeAvailability(pr({ conflicts: true })).reason).toMatch(/conflicts/);
		expect(mergeAvailability(pr({ merge_state: 'DIRTY' })).ok).toBe(false);
		// BLOCKED / UNSTABLE may still be merged; GitHub decides.
		expect(mergeAvailability(pr({ merge_state: 'BLOCKED' })).ok).toBe(true);
	});

	it('picks squash when allowed, else the first allowed method', () => {
		expect(defaultMergeMethod(pr())).toBe('squash');
		expect(defaultMergeMethod(pr({ merge_methods: ['merge', 'rebase'] }))).toBe('merge');
		expect(defaultMergeMethod(pr({ merge_methods: [] }))).toBeNull();
	});

	it('formats sha, merge state and messages', () => {
		expect(shortSha(SHA)).toBe('abcdef1');
		expect(shortSha(undefined)).toBe('');
		expect(mergeStateLabel('BEHIND')).toMatch(/behind/);
		expect(mergeStateLabel(undefined)).toMatch(/not computed/);
		const res = { ok: true, pr: null, removed: true, state: 'MERGED' };
		expect(prDoneMessage('merge', res)).toBe('Merged');
		expect(prDoneMessage('merge', { ...res, state: 'OPEN', removed: false })).toMatch(/not merged/);
	});

	it('parses reviewer lists', () => {
		expect(parseReviewers(' @octocat, acme/core  octocat\nbyron ')).toEqual(['octocat', 'acme/core', 'byron']);
		expect(parseReviewers('  ,  ')).toEqual([]);
	});

	it('replaces or drops a PR row', () => {
		const data = work([], [pr(), pr({ number: 2 })]);
		const fresh = pr({ title: 'new' });
		const replaced = applyPrResult(data, 'acme/app', 1, { ok: true, pr: fresh, removed: false, state: 'OPEN' });
		expect(replaced.github.prs.map((p) => p.title)).toEqual(['new', 'PR']);
		expect(data.github.prs[0].title).toBe('PR');
		const dropped = applyPrResult(data, 'acme/app', 1, { ok: true, pr: null, removed: true, state: 'MERGED' });
		expect(dropped.github.prs.map((p) => p.number)).toEqual([2]);
	});
});

describe('Linear actions', () => {
	it('derives the team key', () => {
		expect(teamOf('TRAD-12')).toBe('TRAD');
	});

	it('replaces, adds or drops an issue row', () => {
		const data = work([issue('T-1'), issue('T-2')], []);
		const done = issue('T-1', { state: 'In Progress', state_type: 'started' });
		expect(applyIssueResult(data, 'T-1', { ok: true, issue: done, removed: false }).linear.issues[0].state).toBe(
			'In Progress'
		);
		expect(
			applyIssueResult(data, 'T-1', { ok: true, issue: done, removed: true }).linear.issues.map((i) => i.identifier)
		).toEqual(['T-2']);
		expect(applyIssueResult(data, 'T-3', { ok: true, issue: issue('T-3'), removed: false }).linear.issues).toHaveLength(3);
	});
});
