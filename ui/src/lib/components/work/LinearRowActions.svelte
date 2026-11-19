<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Actions on one Linear row of My work: a state picker and a "…" menu
	(assign to me, priority, comment). Every action runs only on the user's
	click, through one Claude Code turn that Laya checks against Linear before
	it reports success.
-->
<script module lang="ts">
	import { engineApi } from '$lib/api/engine';
	import type { LinearState } from '$lib/work/actions';

	// One states request per team and page session; the engine caches them too.
	const statesByTeam = new Map<string, Promise<LinearState[]>>();

	function loadStates(team: string, refresh = false): Promise<LinearState[]> {
		if (refresh || !statesByTeam.has(team)) {
			const p = engineApi.getLinearStates(team, refresh).then((r) => r.states);
			p.catch(() => statesByTeam.delete(team));
			statesByTeam.set(team, p);
		}
		return statesByTeam.get(team)!;
	}
</script>

<script lang="ts">
	import RowPanel from './RowPanel.svelte';
	import { PRIORITY_NAMES, type WorkIssue } from '$lib/work/work';
	import { teamOf, type LinearAction, type LinearActionResult } from '$lib/work/actions';

	interface Props {
		issue: WorkIssue;
		onresult: (result: LinearActionResult) => void;
	}

	let { issue, onresult }: Props = $props();

	type Panel = 'state' | 'menu' | 'priority' | 'comment' | 'error' | null;
	let panel = $state<Panel>(null);
	let running = $state<LinearAction | null>(null);
	let done = $state<string | null>(null);
	let error = $state<string | null>(null);
	let states = $state<LinearState[] | null>(null);
	let statesError = $state<string | null>(null);
	let comment = $state('');
	let stateBtn = $state<HTMLButtonElement | null>(null);
	let menuBtn = $state<HTMLButtonElement | null>(null);
	let doneTimer: ReturnType<typeof setTimeout> | undefined;

	const RUNNING_LABEL: Record<LinearAction, string> = {
		state: 'Changing state…',
		assign_me: 'Assigning…',
		priority: 'Setting priority…',
		comment: 'Posting comment…'
	};
	const DONE_LABEL: Record<LinearAction, string> = {
		state: 'State changed',
		assign_me: 'Assigned to you',
		priority: 'Priority set',
		comment: 'Comment posted'
	};

	async function openStates() {
		if (running) return;
		panel = panel === 'state' ? null : 'state';
		if (panel !== 'state' || states) return;
		statesError = null;
		try {
			states = await loadStates(teamOf(issue.identifier));
		} catch (e: any) {
			statesError = e?.message ?? 'Could not load the workflow states';
		}
	}

	async function reloadStates() {
		states = null;
		statesError = null;
		try {
			states = await loadStates(teamOf(issue.identifier), true);
		} catch (e: any) {
			statesError = e?.message ?? 'Could not load the workflow states';
		}
	}

	async function run(action: LinearAction, value?: unknown) {
		if (running) return;
		panel = null;
		running = action;
		error = null;
		done = null;
		clearTimeout(doneTimer);
		try {
			const res = await engineApi.runLinearAction(issue.identifier, action, value);
			done = DONE_LABEL[action];
			if (action === 'comment') comment = '';
			// A completed issue leaves the list; keep the confirmation visible first.
			if (res.removed) setTimeout(() => onresult(res), 1500);
			else onresult(res);
			doneTimer = setTimeout(() => (done = null), 4000);
		} catch (e: any) {
			error = e?.message ?? 'The action failed';
			panel = 'error';
		} finally {
			running = null;
		}
	}

	function submitComment() {
		if (comment.trim()) run('comment', comment);
	}

	const item =
		'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-surface-200 transition-colors hover:bg-surface-800 disabled:opacity-40';
</script>

<div class="relative z-10 flex shrink-0 items-center gap-1">
	{#if running}
		<span class="flex items-center gap-1 text-laya-micro text-surface-400" aria-live="polite">
			<span class="h-2.5 w-2.5 animate-spin rounded-full border border-surface-500 border-t-transparent"></span>
			{RUNNING_LABEL[running]}
		</span>
	{:else if done}
		<span class="text-laya-micro text-emerald-300" aria-live="polite">{done}</span>
	{:else if error}
		<button
			type="button"
			class="rounded px-1 text-laya-micro text-rose-300 hover:bg-rose-500/10"
			onclick={() => (panel = 'error')}
			title={error}>Failed</button
		>
	{/if}

	<button
		type="button"
		bind:this={stateBtn}
		onclick={openStates}
		disabled={!!running}
		class="max-w-[7.5rem] truncate rounded border border-surface-700/70 px-1.5 py-px text-laya-micro text-surface-300 transition-colors hover:border-surface-600 hover:text-surface-100 disabled:opacity-50"
		title="Change state"
		aria-haspopup="menu">{issue.state || 'State'}</button
	>
	<button
		type="button"
		bind:this={menuBtn}
		onclick={() => (panel = panel === 'menu' || panel === 'priority' || panel === 'comment' ? null : 'menu')}
		disabled={!!running}
		class="flex h-5 w-5 items-center justify-center rounded text-surface-500 transition-colors hover:bg-surface-700 hover:text-surface-100 disabled:opacity-50"
		title="Actions"
		aria-label="Actions for {issue.identifier}"
		aria-haspopup="menu"
	>
		<svg viewBox="0 0 16 16" class="h-3.5 w-3.5" fill="currentColor"><circle cx="3.5" cy="8" r="1.3" /><circle cx="8" cy="8" r="1.3" /><circle cx="12.5" cy="8" r="1.3" /></svg>
	</button>
</div>

{#if panel === 'state'}
	<RowPanel anchor={stateBtn} onclose={() => (panel = null)} label="Change state of {issue.identifier}" width={200}>
		{#if statesError}
			<p class="px-2 py-1 text-laya-micro text-rose-300">{statesError}</p>
			<button type="button" class={item} onclick={reloadStates}>Try again</button>
		{:else if !states}
			<p class="px-2 py-1.5 text-laya-micro text-surface-400">Loading {teamOf(issue.identifier)} states…</p>
		{:else}
			<div role="menu">
				{#each states as s (s.id)}
					<button
						type="button"
						role="menuitem"
						class={item}
						disabled={s.name === issue.state}
						onclick={() => run('state', s.name)}
					>
						<span class="flex-1 truncate">{s.name}</span>
						{#if s.name === issue.state}<span class="text-laya-micro text-surface-500">current</span>{/if}
					</button>
				{/each}
			</div>
		{/if}
	</RowPanel>
{:else if panel === 'menu'}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Actions for {issue.identifier}" width={190}>
		<div role="menu">
			{#if !issue.assigned_to_me}
				<button type="button" role="menuitem" class={item} onclick={() => run('assign_me')}>Assign to me</button>
			{/if}
			<button type="button" role="menuitem" class={item} onclick={() => (panel = 'priority')}>Set priority…</button>
			<button type="button" role="menuitem" class={item} onclick={() => (panel = 'comment')}>Comment…</button>
		</div>
	</RowPanel>
{:else if panel === 'priority'}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Priority of {issue.identifier}" width={170}>
		<div role="menu">
			{#each [1, 2, 3, 4, 0] as p (p)}
				<button type="button" role="menuitem" class={item} disabled={p === issue.priority} onclick={() => run('priority', p)}>
					<span class="flex-1">{PRIORITY_NAMES[p]}</span>
					{#if p === issue.priority}<span class="text-laya-micro text-surface-500">current</span>{/if}
				</button>
			{/each}
		</div>
	</RowPanel>
{:else if panel === 'comment'}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Comment on {issue.identifier}" width={320}>
		<div class="p-1.5">
			<p class="mb-1 text-laya-micro text-surface-400">Comment on {issue.identifier} (Markdown)</p>
			<!-- svelte-ignore a11y_autofocus -->
			<textarea
				bind:value={comment}
				autofocus
				rows="4"
				class="w-full resize-y rounded border border-surface-700 bg-surface-950 px-2 py-1 text-laya-secondary text-surface-100 focus:border-surface-500 focus:outline-none"
				onkeydown={(e) => {
					if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submitComment();
				}}
			></textarea>
			<div class="mt-1.5 flex items-center justify-end gap-2">
				<button type="button" class="rounded px-2 py-0.5 text-laya-micro text-surface-400 hover:text-surface-100" onclick={() => (panel = null)}>Cancel</button>
				<button
					type="button"
					class="rounded bg-laya-orange/90 px-2.5 py-0.5 text-laya-micro font-medium text-surface-950 hover:bg-laya-orange disabled:opacity-40"
					disabled={!comment.trim()}
					onclick={submitComment}>Post comment</button
				>
			</div>
		</div>
	</RowPanel>
{:else if panel === 'error' && error}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Action failed" width={300}>
		<div class="p-1.5">
			<p class="whitespace-pre-wrap break-words text-laya-micro text-rose-300">{error}</p>
			<div class="mt-1.5 flex justify-end">
				<button type="button" class="rounded px-2 py-0.5 text-laya-micro text-surface-400 hover:text-surface-100" onclick={() => { error = null; panel = null; }}>Dismiss</button>
			</div>
		</div>
	</RowPanel>
{/if}
