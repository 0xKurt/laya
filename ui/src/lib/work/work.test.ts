// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	ciStatus,
	filterIssues,
	groupIssues,
	groupPrs,
	isOverdue,
	isRefreshing,
	linearUrlForKey,
	oldestFetch,
	otherAssignee,
	parseIssueFilter,
	prGroup,
	reviewState,
	type MyWork,
	type WorkIssue,
	type WorkPr,
	type WorkSection
} from './work';

function pr(over: Partial<WorkPr> = {}): WorkPr {
	return {
		repo: 'acme/app',
		number: 1,
		title: 'PR',
		url: 'https://github.com/acme/app/pull/1',
		draft: false,
		created_at: '2026-11-01T00:00:00Z',
		updated_at: '2026-11-02T00:00:00Z',
		base: 'main',
		branch: 'kurt/x',
		mergeable: 'MERGEABLE',
		conflicts: false,
		review_decision: 'REVIEW_REQUIRED',
		requested_reviewers: 1,
		reviews: [],
		approvals: 0,
		changes_requested: 0,
		unresolved_threads: 0,
		checks: { state: 'success', failing: 0, pending: 0, total: 2 },
		linear_key: null,
		...over
	};
}

function issue(over: Partial<WorkIssue> = {}): WorkIssue {
	return {
		identifier: 'T-1',
		title: 'Issue',
		url: 'https://linear.app/acme/issue/T-1/issue',
		state: 'In Progress',
		state_type: 'started',
		priority: 0,
		project: null,
		cycle: null,
		due_date: null,
		updated_at: '2026-11-01T00:00:00Z',
		labels: [],
		assignee: 'Kurt',
		assigned_to_me: true,
		created_by_me: false,
		...over
	};
}

describe('pull request groups', () => {
	it('puts changes requested, failing CI and conflicts first', () => {
		expect(prGroup(pr({ review_decision: 'CHANGES_REQUESTED' }))).toBe('action');
		expect(prGroup(pr({ checks: { state: 'failure', failing: 1, pending: 0, total: 2 } }))).toBe('action');
		expect(prGroup(pr({ conflicts: true, draft: true }))).toBe('action');
	});

	it('separates waiting, ready and drafts', () => {
		expect(prGroup(pr())).toBe('waiting');
		expect(prGroup(pr({ review_decision: 'APPROVED' }))).toBe('ready');
		expect(prGroup(pr({ review_decision: null, approvals: 1 }))).toBe('ready');
		expect(prGroup(pr({ review_decision: 'APPROVED', checks: { state: 'pending', failing: 0, pending: 1, total: 2 } }))).toBe(
			'waiting'
		);
		expect(prGroup(pr({ draft: true }))).toBe('draft');
	});

	it('orders groups action → waiting → ready → drafts, newest first', () => {
		const groups = groupPrs([
			pr({ number: 1, review_decision: 'APPROVED' }),
			pr({ number: 2, updated_at: '2026-11-01T00:00:00Z' }),
			pr({ number: 3, updated_at: '2026-11-03T00:00:00Z' }),
			pr({ number: 4, conflicts: true }),
			pr({ number: 5, draft: true })
		]);
		expect(groups.map((g) => g.id)).toEqual(['action', 'waiting', 'ready', 'draft']);
		expect(groups[1].prs.map((p) => p.number)).toEqual([3, 2]);
	});
});

describe('pull request status', () => {
	it('maps the review state with counts', () => {
		expect(reviewState(pr({ review_decision: 'APPROVED', approvals: 2 }))).toEqual({ kind: 'approved', label: 'Approved', count: 2 });
		expect(reviewState(pr({ changes_requested: 1 })).kind).toBe('changes');
		expect(reviewState(pr({ requested_reviewers: 3 }))).toEqual({ kind: 'required', label: 'Review required', count: 3 });
		expect(reviewState(pr({ review_decision: null, requested_reviewers: 0 })).kind).toBe('none');
	});

	it('maps the check rollup to a dot', () => {
		expect(ciStatus({ state: 'failure', failing: 2, pending: 0, total: 5 })).toEqual({ color: 'red', label: '2 checks failing' });
		expect(ciStatus({ state: 'pending', failing: 0, pending: 1, total: 5 }).color).toBe('amber');
		expect(ciStatus({ state: 'success', failing: 0, pending: 0, total: 5 }).color).toBe('green');
		expect(ciStatus({ state: 'none', failing: 0, pending: 0, total: 0 }).color).toBe('none');
	});
});

describe('Linear issues', () => {
	const issues = [
		issue({ identifier: 'A', state_type: 'backlog' }),
		issue({ identifier: 'B', priority: 3 }),
		issue({ identifier: 'C', priority: 1 }),
		issue({ identifier: 'D', priority: 0, due_date: '2026-11-20' }),
		issue({ identifier: 'E', state_type: 'unstarted', assigned_to_me: false, created_by_me: true, assignee: null }),
		issue({ identifier: 'F', state_type: 'unstarted', assigned_to_me: true, created_by_me: true })
	];

	it('filters by assigned, created and all', () => {
		expect(filterIssues(issues, 'assigned').map((i) => i.identifier)).toEqual(['A', 'B', 'C', 'D', 'F']);
		expect(filterIssues(issues, 'created').map((i) => i.identifier)).toEqual(['E', 'F']);
		expect(filterIssues(issues, 'all')).toHaveLength(6);
		expect(parseIssueFilter('created')).toBe('created');
		expect(parseIssueFilter('junk')).toBe('assigned');
		expect(parseIssueFilter(null)).toBe('assigned');
	});

	it('groups by state type and sorts by priority, due date, then update', () => {
		const groups = groupIssues(issues);
		expect(groups.map((g) => g.id)).toEqual(['started', 'unstarted', 'backlog']);
		expect(groups[0].issues.map((i) => i.identifier)).toEqual(['C', 'B', 'D']);
	});

	it('shows the assignee only on issues created for someone else', () => {
		expect(otherAssignee(issues[4])).toBe('Unassigned');
		expect(otherAssignee(issue({ assigned_to_me: false, created_by_me: true, assignee: 'Byron' }))).toBe('Byron');
		expect(otherAssignee(issues[5])).toBeNull();
		expect(otherAssignee(issues[0])).toBeNull();
	});

	it('marks overdue dates and builds Linear links for PR keys', () => {
		const now = new Date(2026, 10, 19, 12);
		expect(isOverdue('2026-11-18', now)).toBe(true);
		expect(isOverdue('2026-11-19', now)).toBe(false);
		expect(isOverdue(null, now)).toBe(false);
		expect(linearUrlForKey('A', issues)).toBe('https://linear.app/acme/issue/T-1/issue');
		expect(linearUrlForKey('TRAD-9', issues)).toBe('https://linear.app/acme/issue/TRAD-9');
		expect(linearUrlForKey('TRAD-9', [])).toBeNull();
	});
});

describe('stored sections', () => {
	function section(over: Partial<WorkSection> = {}): WorkSection {
		return { connected: true, error: null, note: null, fetched_at: null, attempted_at: null, cached: false, refreshing: false, ...over };
	}
	function work(github: Partial<WorkSection>, linear: Partial<WorkSection>): MyWork {
		return { github: { ...section(github), prs: [] }, linear: { ...section(linear), issues: [] } };
	}

	it('is refreshing while any connected section refreshes', () => {
		expect(isRefreshing(null)).toBe(false);
		expect(isRefreshing(work({}, {}))).toBe(false);
		expect(isRefreshing(work({}, { refreshing: true }))).toBe(true);
		expect(isRefreshing(work({ connected: false, refreshing: true }, {}))).toBe(false);
	});

	it('reports the older successful fetch of the connected sections', () => {
		expect(oldestFetch(null)).toBeNull();
		expect(oldestFetch(work({ fetched_at: '2026-11-20T08:00:00+00:00' }, { fetched_at: '2026-11-20T07:00:00+00:00' }))).toBe(
			'2026-11-20T07:00:00+00:00'
		);
		expect(oldestFetch(work({ fetched_at: '2026-11-20T08:00:00+00:00' }, { connected: false, fetched_at: '2026-01-01T00:00:00+00:00' }))).toBe(
			'2026-11-20T08:00:00+00:00'
		);
	});
});
