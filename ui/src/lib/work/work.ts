// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// "My work" view: the user's open Linear issues and own open GitHub pull
// requests (GET /claude-code/my-work). Pure grouping, sorting and status
// mapping; the page only renders what these functions return.

export interface WorkCheckSummary {
	state: 'success' | 'failure' | 'pending' | 'none';
	failing: number;
	pending: number;
	total: number;
}

export interface WorkPr {
	repo: string;
	number: number;
	title: string;
	url: string;
	draft: boolean;
	created_at: string | null;
	updated_at: string | null;
	base: string;
	branch: string;
	mergeable: 'MERGEABLE' | 'CONFLICTING' | 'UNKNOWN' | string;
	conflicts: boolean;
	review_decision: 'APPROVED' | 'CHANGES_REQUESTED' | 'REVIEW_REQUIRED' | null;
	requested_reviewers: number;
	reviews: { state: string; author: string }[];
	approvals: number;
	changes_requested: number;
	unresolved_threads: number;
	checks: WorkCheckSummary;
	linear_key: string | null;
	author?: string;
	head_sha?: string;
	/** GitHub mergeStateStatus: CLEAN, BEHIND, BLOCKED, DIRTY, UNSTABLE, HAS_HOOKS, DRAFT, UNKNOWN. */
	merge_state?: string;
	/** The merge methods the repository allows, as `gh pr merge` flags. */
	merge_methods?: MergeMethod[];
	delete_branch_on_merge?: boolean;
}

export type MergeMethod = 'squash' | 'merge' | 'rebase';

export interface WorkIssue {
	identifier: string;
	title: string;
	url: string | null;
	state: string;
	state_type: string;
	priority: number;
	project: string | null;
	cycle: string | null;
	due_date: string | null;
	updated_at: string | null;
	labels: string[];
	assignee: string | null;
	assigned_to_me: boolean;
	created_by_me: boolean;
}

export interface WorkSection {
	connected: boolean;
	/** The last refresh's failure (None after a success), with the reason. */
	error: string | null;
	note: string | null;
	/** Last successful refresh. */
	fetched_at: string | null;
	/** Last refresh attempt, successful or not. */
	attempted_at: string | null;
	/** Whether stored data exists. */
	cached: boolean;
	/** A background refresh of this section is running. */
	refreshing: boolean;
}

export interface MyWork {
	github: WorkSection & { prs: WorkPr[] };
	linear: WorkSection & { issues: WorkIssue[] };
}

/** Whether a background refresh of any connected section is running. */
export function isRefreshing(data: MyWork | null): boolean {
	if (!data) return false;
	return (data.github.connected && !!data.github.refreshing) || (data.linear.connected && !!data.linear.refreshing);
}

/** The older of the sections' last successful refreshes (connected sections only). */
export function oldestFetch(data: MyWork | null): string | null {
	if (!data) return null;
	const times = [data.github, data.linear]
		.filter((s) => s.connected && s.fetched_at)
		.map((s) => s.fetched_at as string);
	return times.length ? times.sort()[0] : null;
}

// ── GitHub ──────────────────────────────────────────────────────────────

export type PrGroupId = 'action' | 'waiting' | 'ready' | 'draft';

export const PR_GROUPS: { id: PrGroupId; title: string; hint: string }[] = [
	{ id: 'action', title: 'Needs your action', hint: 'changes requested, failing checks or conflicts' },
	{ id: 'waiting', title: 'Waiting on review', hint: '' },
	{ id: 'ready', title: 'Ready to merge', hint: 'approved and green' },
	{ id: 'draft', title: 'Drafts', hint: '' }
];

export function changesRequested(pr: WorkPr): boolean {
	return pr.review_decision === 'CHANGES_REQUESTED' || pr.changes_requested > 0;
}

export function isApproved(pr: WorkPr): boolean {
	// Repositories without required reviews report no decision; an approving review still counts.
	return pr.review_decision === 'APPROVED' || (pr.review_decision === null && pr.approvals > 0 && !changesRequested(pr));
}

export function prGroup(pr: WorkPr): PrGroupId {
	if (changesRequested(pr) || pr.checks.state === 'failure' || pr.conflicts) return 'action';
	if (pr.draft) return 'draft';
	if (isApproved(pr) && (pr.checks.state === 'success' || pr.checks.state === 'none')) return 'ready';
	return 'waiting';
}

function byUpdatedDesc<T extends { updated_at: string | null }>(a: T, b: T): number {
	return (b.updated_at ?? '').localeCompare(a.updated_at ?? '');
}

/** The pull requests per group, newest update first; empty groups are left out. */
export function groupPrs(prs: WorkPr[]): { id: PrGroupId; title: string; hint: string; prs: WorkPr[] }[] {
	return PR_GROUPS.map((g) => ({ ...g, prs: prs.filter((p) => prGroup(p) === g.id).sort(byUpdatedDesc) })).filter(
		(g) => g.prs.length > 0
	);
}

export type ReviewKind = 'approved' | 'changes' | 'required' | 'none';

export function reviewState(pr: WorkPr): { kind: ReviewKind; label: string; count: number } {
	if (changesRequested(pr)) return { kind: 'changes', label: 'Changes requested', count: pr.changes_requested };
	if (isApproved(pr)) return { kind: 'approved', label: 'Approved', count: pr.approvals };
	if (pr.review_decision === 'REVIEW_REQUIRED' || pr.requested_reviewers > 0)
		return { kind: 'required', label: 'Review required', count: pr.requested_reviewers };
	return { kind: 'none', label: 'No review', count: 0 };
}

export type CiColor = 'green' | 'red' | 'amber' | 'none';

export function ciStatus(checks: WorkCheckSummary): { color: CiColor; label: string } {
	switch (checks.state) {
		case 'failure':
			return { color: 'red', label: checks.failing === 1 ? '1 check failing' : `${checks.failing} checks failing` };
		case 'pending':
			return { color: 'amber', label: checks.pending === 1 ? '1 check running' : `${checks.pending} checks running` };
		case 'success':
			return { color: 'green', label: checks.total === 1 ? '1 check passed' : `All ${checks.total} checks passed` };
		default:
			return { color: 'none', label: 'No checks' };
	}
}

// ── Linear ──────────────────────────────────────────────────────────────

export type IssueFilter = 'assigned' | 'created' | 'all';

export const ISSUE_FILTERS: { id: IssueFilter; label: string }[] = [
	{ id: 'assigned', label: 'Assigned to me' },
	{ id: 'created', label: 'Created by me' },
	{ id: 'all', label: 'All' }
];

export function parseIssueFilter(value: string | null | undefined): IssueFilter {
	return value === 'created' || value === 'all' ? value : 'assigned';
}

export function filterIssues(issues: WorkIssue[], filter: IssueFilter): WorkIssue[] {
	if (filter === 'assigned') return issues.filter((i) => i.assigned_to_me);
	if (filter === 'created') return issues.filter((i) => i.created_by_me);
	return issues;
}

export const ISSUE_GROUPS: { id: string; title: string }[] = [
	{ id: 'started', title: 'In progress' },
	{ id: 'unstarted', title: 'Todo' },
	{ id: 'backlog', title: 'Backlog' },
	{ id: 'triage', title: 'Triage' }
];

// Linear priority values: 1 urgent … 4 low, 0 none (sorted last).
function priorityRank(p: number): number {
	return p >= 1 && p <= 4 ? p : 5;
}

export function compareIssues(a: WorkIssue, b: WorkIssue): number {
	const byPriority = priorityRank(a.priority) - priorityRank(b.priority);
	if (byPriority) return byPriority;
	if (a.due_date !== b.due_date) {
		if (!a.due_date) return 1;
		if (!b.due_date) return -1;
		return a.due_date.localeCompare(b.due_date);
	}
	return byUpdatedDesc(a, b);
}

/** Issues per state type (In progress → Todo → Backlog → Triage); empty groups are left out. */
export function groupIssues(issues: WorkIssue[]): { id: string; title: string; issues: WorkIssue[] }[] {
	const known = new Set(ISSUE_GROUPS.map((g) => g.id));
	const groups = ISSUE_GROUPS.map((g) => ({ ...g, issues: issues.filter((i) => i.state_type === g.id) }));
	const other = issues.filter((i) => !known.has(i.state_type));
	if (other.length) groups.push({ id: 'other', title: 'Other', issues: other });
	return groups.map((g) => ({ ...g, issues: [...g.issues].sort(compareIssues) })).filter((g) => g.issues.length > 0);
}

export const PRIORITY_NAMES = ['No priority', 'Urgent', 'High', 'Medium', 'Low'];

export function priorityName(p: number): string {
	return PRIORITY_NAMES[p] ?? PRIORITY_NAMES[0];
}

/** A due date (YYYY-MM-DD) before today, in local time. */
export function isOverdue(due: string | null, now: Date = new Date()): boolean {
	if (!due) return false;
	const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
	return due.slice(0, 10) < today;
}

export function formatDue(due: string, now: Date = new Date()): string {
	const d = new Date(`${due.slice(0, 10)}T00:00:00`);
	if (Number.isNaN(d.getTime())) return due;
	const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };
	if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
	return d.toLocaleDateString(undefined, opts);
}

/** The assignee shown on an issue the user created for someone else; null for the user's own issues. */
export function otherAssignee(issue: WorkIssue): string | null {
	if (issue.assigned_to_me || !issue.created_by_me) return null;
	return issue.assignee || 'Unassigned';
}

/** The Linear link for a key named by a pull request: the listed issue's URL, else one built from the workspace. */
export function linearUrlForKey(key: string, issues: WorkIssue[]): string | null {
	const hit = issues.find((i) => i.identifier === key);
	if (hit?.url) return hit.url;
	for (const i of issues) {
		const m = i.url?.match(/^https:\/\/linear\.app\/([^/]+)\//);
		if (m) return `https://linear.app/${m[1]}/issue/${key}`;
	}
	return null;
}
