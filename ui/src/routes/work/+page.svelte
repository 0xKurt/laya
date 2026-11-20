<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { onMount } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import { lastMessage } from '$lib/stores/websocket';
	import PlatformIcon from '$lib/components/settings/PlatformIcon.svelte';
	import { timeAgo } from '$lib/utils/datetime';
	import LinearRowActions from '$lib/components/work/LinearRowActions.svelte';
	import PrRowActions from '$lib/components/work/PrRowActions.svelte';
	import { applyIssueResult, applyPrResult } from '$lib/work/actions';
	import ReferenceInClaude from '$lib/components/claude/ReferenceInClaude.svelte';
	import { linearReference, prReference } from '$lib/claude/reference';
	import {
		ISSUE_FILTERS,
		ciStatus,
		filterIssues,
		formatDue,
		groupIssues,
		groupPrs,
		isOverdue,
		isRefreshing,
		linearUrlForKey,
		oldestFetch,
		otherAssignee,
		parseIssueFilter,
		priorityName,
		reviewState,
		type IssueFilter,
		type MyWork,
		type WorkSection
	} from '$lib/work/work';

	let data = $state<MyWork | null>(null);
	let loading = $state(true);
	// A click on Refresh until the engine reports the refresh as running.
	let requested = $state(false);
	let error = $state<string | null>(null);
	const refreshing = $derived(requested || isRefreshing(data));

	// ── Linear filter (remembered per device) ──
	const FILTER_KEY = 'laya.work.linearFilter';
	function storedFilter(): IssueFilter {
		try {
			return parseIssueFilter(localStorage.getItem(FILTER_KEY));
		} catch {
			return 'assigned';
		}
	}
	let issueFilter = $state<IssueFilter>(storedFilter());
	function setFilter(next: IssueFilter) {
		issueFilter = next;
		try {
			localStorage.setItem(FILTER_KEY, next);
		} catch {
			// storage unavailable: the choice lasts for this session only
		}
	}

	const allIssues = $derived(data?.linear.issues ?? []);
	const shownIssues = $derived(filterIssues(allIssues, issueFilter));
	const issueGroups = $derived(groupIssues(shownIssues));
	const prs = $derived(data?.github.prs ?? []);
	const prGroups = $derived(groupPrs(prs));
	const filterCounts = $derived(
		Object.fromEntries(ISSUE_FILTERS.map((f) => [f.id, filterIssues(allIssues, f.id).length])) as Record<IssueFilter, number>
	);
	const lastFetched = $derived(oldestFetch(data));

	// timeAgo labels go stale while the page stays open; a minute tick re-renders them.
	let tick = $state(0);
	function ago(iso: string | null): string {
		void tick;
		return timeAgo(iso, { weeks: true });
	}

	// The engine returns the stored sections at once and refreshes stale ones
	// in the background; while a refresh runs the page re-reads every few
	// seconds, and a `my_work_updated` message re-reads at once.
	const POLL_MS = 4_000;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;

	async function load(refresh = false) {
		if (refresh) requested = true;
		try {
			data = await engineApi.getMyWork(refresh);
			error = null;
		} catch (e: any) {
			error = e?.message ?? 'Could not load your work';
		} finally {
			loading = false;
			requested = false;
			schedulePoll();
		}
	}

	function schedulePoll() {
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = isRefreshing(data) ? setTimeout(() => load(), POLL_MS) : null;
	}

	onMount(() => {
		load();
		const timer = setInterval(() => (tick += 1), 60_000);
		const unsubscribe = lastMessage.subscribe((msg) => {
			if (msg?.type === 'my_work_updated' && !loading) load();
		});
		return () => {
			clearInterval(timer);
			unsubscribe();
			if (pollTimer) clearTimeout(pollTimer);
		};
	});

	const CI_DOT: Record<string, string> = {
		green: 'bg-emerald-400',
		red: 'bg-rose-400',
		amber: 'bg-amber-400',
		none: 'bg-surface-600'
	};
	const REVIEW_CHIP: Record<string, string> = {
		approved: 'bg-emerald-500/12 text-emerald-300',
		changes: 'bg-rose-500/12 text-rose-300',
		required: 'bg-surface-700/60 text-surface-300',
		none: 'text-surface-500'
	};
</script>

{#snippet priorityIcon(p: number)}
	<span class="flex h-4 w-4 shrink-0 items-center justify-center" title={priorityName(p)}>
		{#if p === 1}
			<svg viewBox="0 0 16 16" class="h-3.5 w-3.5"><rect x="1" y="1" width="14" height="14" rx="3" class="fill-laya-coral" /><path d="M8 4v5M8 11.5v.5" stroke="var(--color-surface-950, #111)" stroke-width="2" stroke-linecap="round" /></svg>
		{:else if p >= 2 && p <= 4}
			<svg viewBox="0 0 16 16" class="h-3.5 w-3.5">
				{#each [0, 1, 2] as bar (bar)}
					<rect
						x={2 + bar * 4.5}
						y={10 - bar * 3.5}
						width="3"
						height={4 + bar * 3.5}
						rx="1"
						class={bar < 5 - p ? 'fill-surface-300' : 'fill-surface-700'}
					/>
				{/each}
			</svg>
		{:else}
			<svg viewBox="0 0 16 16" class="h-3.5 w-3.5"><path d="M3 8h2M7 8h2M11 8h2" class="stroke-surface-600" stroke-width="1.5" stroke-linecap="round" /></svg>
		{/if}
	</span>
{/snippet}

{#snippet sectionMeta(section: WorkSection)}
	{#if section.connected}
		{#if section.refreshing}
			<span class="flex items-center gap-1 text-laya-micro text-surface-500" title="Refreshing in the background">
				<svg class="h-3 w-3 animate-spin" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
					<path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
				</svg>
				Refreshing…
			</span>
		{:else if section.fetched_at}
			<span class="text-laya-micro text-surface-600" title={section.fetched_at}>Updated {ago(section.fetched_at)}</span>
		{/if}
	{/if}
{/snippet}

{#snippet sectionState(section: WorkSection, label: string, empty: string, count: number, waiting: string)}
	{#if !section.connected}
		<div class="px-4 py-10 text-center">
			<p class="text-laya-secondary text-surface-300">{label} is not connected.</p>
			<p class="mt-1 text-laya-micro text-surface-500">
				Connect {label} via coding agent in <a href="/settings" class="text-laya-orange/90 hover:underline">Settings → Integrations</a>.
			</p>
		</div>
	{:else}
		{#if section.error}
			<p class="mx-3 mb-2 rounded-md bg-rose-500/10 px-2.5 py-1.5 text-laya-micro text-rose-300" title={section.attempted_at ? `Last try ${section.attempted_at}` : ''}>
				Could not refresh {label}: {section.error}
				{#if section.fetched_at}<span class="text-rose-300/70"> Showing data from {ago(section.fetched_at)}.</span>{/if}
			</p>
		{/if}
		{#if section.note}
			<p class="mx-3 mb-2 rounded-md bg-amber-500/10 px-2.5 py-1.5 text-laya-micro text-amber-300">{section.note}</p>
		{/if}
		{#if count === 0 && !section.cached && section.refreshing}
			<p class="px-4 pb-2 text-laya-micro text-surface-500">{waiting}</p>
			{#each [0, 1, 2] as i (i)}
				<div class="mx-3 my-1 h-7 animate-pulse rounded-md bg-surface-800/40"></div>
			{/each}
		{:else if count === 0 && !section.error}
			<p class="px-4 py-10 text-center text-laya-secondary text-surface-500">{empty}</p>
		{/if}
	{/if}
{/snippet}

<div class="-m-4 flex h-[calc(100%+2rem)] flex-col overflow-hidden">
	<header class="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-5 pb-2 pt-4">
		<h1 class="text-base font-semibold text-surface-100">My work</h1>
		{#if data}
			<span class="text-laya-secondary text-surface-400">
				{filterCounts.assigned} {filterCounts.assigned === 1 ? 'issue' : 'issues'} assigned · {prs.length}
				{prs.length === 1 ? 'pull request' : 'pull requests'}
			</span>
		{/if}
		<span class="flex-1"></span>
		{#if lastFetched}
			<span class="text-laya-micro text-surface-500" title={lastFetched}>Updated {ago(lastFetched)}</span>
		{/if}
		<button
			type="button"
			onclick={() => load(true)}
			disabled={refreshing || loading}
			title="Refresh both sections in the background"
			class="flex items-center gap-1.5 rounded-md border border-surface-700 px-2.5 py-1 text-laya-micro font-medium text-surface-300 transition-colors hover:border-surface-600 hover:bg-surface-800 hover:text-surface-100 disabled:opacity-50"
		>
			<svg class="h-3 w-3 {refreshing ? 'animate-spin' : ''}" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
			</svg>
			{refreshing ? 'Refreshing…' : 'Refresh'}
		</button>
	</header>

	{#if error}
		<div class="mx-5 mb-2 rounded-lg bg-rose-500/15 px-3 py-2 text-laya-secondary text-rose-300">{error}</div>
	{/if}

	<div class="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto px-4 pb-4 xl:grid-cols-2 xl:overflow-hidden">
		<!-- Linear -->
		<section class="flex min-h-0 flex-col rounded-xl border border-surface-800 bg-surface-900/40" aria-label="Linear issues">
			<div class="flex flex-wrap items-center gap-2 px-4 pb-2 pt-3">
				<span class="text-surface-400"><PlatformIcon platform="linear" size={14} /></span>
				<h2 class="text-laya-secondary font-semibold text-surface-200">Linear</h2>
				<span class="text-laya-micro text-surface-500">{shownIssues.length}</span>
				{#if data}{@render sectionMeta(data.linear)}{/if}
				<span class="flex-1"></span>
				<div class="flex rounded-md bg-surface-800/60 p-0.5" role="group" aria-label="Which issues">
					{#each ISSUE_FILTERS as f (f.id)}
						<button
							type="button"
							onclick={() => setFilter(f.id)}
							aria-pressed={issueFilter === f.id}
							class="rounded px-2 py-0.5 text-laya-micro font-medium transition-colors
								{issueFilter === f.id ? 'bg-surface-700 text-surface-100' : 'text-surface-400 hover:text-surface-200'}"
						>{f.label}{#if data?.linear.connected}<span class="ml-1 text-surface-500">{filterCounts[f.id]}</span>{/if}</button>
					{/each}
				</div>
			</div>

			<div class="min-h-0 flex-1 overflow-y-auto pb-2">
				{#if loading}
					{#each [0, 1, 2, 3, 4] as i (i)}
						<div class="mx-3 my-1 h-7 animate-pulse rounded-md bg-surface-800/40"></div>
					{/each}
				{:else if data}
					{@render sectionState(
						data.linear,
						'Linear',
						issueFilter === 'created' ? 'No open issues created by you.' : 'No open issues. Nice.',
						shownIssues.length,
						'Reading Linear through your coding agent; this can take a minute.'
					)}
					{#each issueGroups as group (group.id)}
						<h3 class="sticky top-0 z-10 flex items-baseline gap-2 bg-surface-900/95 px-4 pb-1 pt-3 text-laya-micro font-semibold uppercase tracking-wide text-surface-500 backdrop-blur">
							{group.title}<span class="font-normal normal-case tracking-normal text-surface-600">{group.issues.length}</span>
						</h3>
						<ul>
							{#each group.issues as issue (issue.identifier)}
								{@const overdue = isOverdue(issue.due_date)}
								{@const other = otherAssignee(issue)}
								<li class="group/ref mx-1.5 flex items-center gap-1.5 rounded-md pr-2 transition-colors hover:bg-surface-800/60">
									<a
										href={issue.url ?? undefined}
										class="group flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-2.5 py-1.5"
										title={`${issue.identifier} · ${issue.state}${issue.labels.length ? ' · ' + issue.labels.join(', ') : ''}`}
									>
										{@render priorityIcon(issue.priority)}
										<span class="w-[4.75rem] shrink-0 font-mono text-laya-micro text-surface-500">{issue.identifier}</span>
										<span class="min-w-0 flex-1 truncate text-laya-secondary text-surface-200 group-hover:text-surface-50">{issue.title}</span>
										{#if other}
											<span class="shrink-0 rounded bg-surface-800 px-1.5 py-px text-laya-micro text-surface-400" title="Assignee">{other}</span>
										{/if}
										{#if issue.project}
											<span class="hidden max-w-[11rem] shrink-0 truncate text-laya-micro text-surface-500 2xl:inline" title={issue.project}>{issue.project}</span>
										{/if}
										{#if issue.due_date}
											<span
												class="shrink-0 text-laya-micro {overdue ? 'font-medium text-rose-300' : 'text-surface-400'}"
												title={overdue ? `Overdue: due ${issue.due_date}` : `Due ${issue.due_date}`}
											>{formatDue(issue.due_date)}</span>
										{/if}
										<span class="w-12 shrink-0 text-right text-laya-micro text-surface-600" title={issue.updated_at ?? ''}>{ago(issue.updated_at)}</span>
									</a>
									<ReferenceInClaude
										reference={() => linearReference(issue)}
										class="h-6 w-6 shrink-0 text-surface-400 opacity-0 hover:text-laya-orange focus-visible:opacity-100 group-hover/ref:opacity-100"
									/>
									<LinearRowActions {issue} onresult={(r) => data && (data = applyIssueResult(data, issue.identifier, r))} />
								</li>
							{/each}
						</ul>
					{/each}
				{/if}
			</div>
		</section>

		<!-- GitHub -->
		<section class="flex min-h-0 flex-col rounded-xl border border-surface-800 bg-surface-900/40" aria-label="GitHub pull requests">
			<div class="flex items-center gap-2 px-4 pb-2 pt-3">
				<span class="text-surface-400"><PlatformIcon platform="github" size={14} /></span>
				<h2 class="text-laya-secondary font-semibold text-surface-200">Your pull requests</h2>
				<span class="text-laya-micro text-surface-500">{prs.length}</span>
				{#if data}{@render sectionMeta(data.github)}{/if}
			</div>

			<div class="min-h-0 flex-1 overflow-y-auto pb-2">
				{#if loading}
					{#each [0, 1, 2, 3, 4] as i (i)}
						<div class="mx-3 my-1 h-10 animate-pulse rounded-md bg-surface-800/40"></div>
					{/each}
				{:else if data}
					{@render sectionState(data.github, 'GitHub', 'No open pull requests authored by you.', prs.length, 'Reading your pull requests from GitHub…')}
					{#each prGroups as group (group.id)}
						<h3 class="sticky top-0 z-10 flex items-baseline gap-2 bg-surface-900/95 px-4 pb-1 pt-3 text-laya-micro font-semibold uppercase tracking-wide backdrop-blur
							{group.id === 'action' ? 'text-laya-orange/90' : group.id === 'ready' ? 'text-emerald-300/80' : 'text-surface-500'}">
							{group.title}<span class="font-normal normal-case tracking-normal text-surface-600">{group.prs.length}{group.hint ? ` · ${group.hint}` : ''}</span>
						</h3>
						<ul>
							{#each group.prs as pr (pr.url)}
								{@const ci = ciStatus(pr.checks)}
								{@const review = reviewState(pr)}
								{@const keyUrl = pr.linear_key ? linearUrlForKey(pr.linear_key, allIssues) : null}
								<li class="group relative mx-1.5 rounded-md px-2.5 py-1.5 transition-colors hover:bg-surface-800/60">
									<div class="flex items-center gap-2">
										<span class="h-2 w-2 shrink-0 rounded-full {CI_DOT[ci.color]}" title={ci.label}></span>
										<a
											href={pr.url}
											class="min-w-0 flex-1 truncate text-laya-secondary text-surface-200 after:absolute after:inset-0 group-hover:text-surface-50"
											title={pr.title}
										>{pr.title}</a>
										<ReferenceInClaude
											reference={() => prReference(pr)}
											class="relative z-10 h-5 w-5 shrink-0 text-surface-400 opacity-0 hover:text-laya-orange focus-visible:opacity-100 group-hover:opacity-100"
										/>
										<PrRowActions {pr} onresult={(r) => data && (data = applyPrResult(data, pr.repo, pr.number, r))} />
										<span class="w-12 shrink-0 text-right text-laya-micro text-surface-600" title={pr.updated_at ?? ''}>{ago(pr.updated_at)}</span>
									</div>
									<div class="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 pl-4 text-laya-micro">
										<span class="font-mono text-surface-500">{pr.repo.split('/').pop()}#{pr.number}</span>
										{#if pr.draft}
											<span class="rounded border border-surface-700 px-1 text-surface-400">Draft</span>
										{/if}
										{#if ci.color === 'red'}
											<span class="text-rose-300">{ci.label}</span>
										{:else if ci.color === 'amber'}
											<span class="text-amber-300/90">{ci.label}</span>
										{/if}
										{#if review.kind !== 'none'}
											<span class="rounded px-1.5 py-px {REVIEW_CHIP[review.kind]}">
												{review.label}{review.count > 1 ? ` · ${review.count}` : ''}
											</span>
										{/if}
										{#if pr.conflicts}
											<span class="rounded bg-rose-500/12 px-1.5 py-px text-rose-300">Conflicts</span>
										{/if}
										{#if pr.unresolved_threads > 0}
											<span class="text-surface-400" title="Unresolved review threads">{pr.unresolved_threads} unresolved</span>
										{/if}
										{#if pr.linear_key}
											{#if keyUrl}
												<a href={keyUrl} class="relative z-10 font-mono text-surface-400 hover:text-laya-orange hover:underline">{pr.linear_key}</a>
											{:else}
												<span class="font-mono text-surface-500">{pr.linear_key}</span>
											{/if}
										{/if}
										{#if pr.base && !['main', 'master'].includes(pr.base)}
											<span class="text-surface-600" title="Base branch">→ {pr.base}</span>
										{/if}
									</div>
								</li>
							{/each}
						</ul>
					{/each}
				{/if}
			</div>
		</section>
	</div>
</div>
