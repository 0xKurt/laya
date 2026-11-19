<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Actions on one of the user's own pull requests in My work: an inline
	"Ready for review" / "Merge" button where it applies and a "…" menu
	(draft, reviewers, comment, update branch, merge). Each runs one `gh`
	command on the user's click; merging always goes through a confirm dialog
	that pins the head commit the user saw.
-->
<script lang="ts">
	import { portal } from '$lib/actions/portal';
	import { engineApi } from '$lib/api/engine';
	import RowPanel from './RowPanel.svelte';
	import { ciStatus, reviewState, type MergeMethod, type WorkPr } from '$lib/work/work';
	import {
		MERGE_METHOD_LABELS,
		defaultMergeMethod,
		mergeAvailability,
		mergeStateLabel,
		parseReviewers,
		prDoneMessage,
		prQuickAction,
		shortSha,
		type PrAction,
		type PrActionRequest,
		type PrActionResult
	} from '$lib/work/actions';

	interface Props {
		pr: WorkPr;
		onresult: (result: PrActionResult) => void;
	}

	let { pr, onresult }: Props = $props();

	type Panel = 'menu' | 'reviewers' | 'comment' | 'error' | null;
	let panel = $state<Panel>(null);
	let running = $state<PrAction | null>(null);
	let done = $state<string | null>(null);
	let error = $state<string | null>(null);
	let comment = $state('');
	let reviewers = $state('');
	let menuBtn = $state<HTMLButtonElement | null>(null);
	let doneTimer: ReturnType<typeof setTimeout> | undefined;

	// Merge dialog: the head commit and settings are captured when it opens.
	let merging = $state<{ sha: string; method: MergeMethod; deleteBranch: boolean; error: string | null } | null>(null);

	const quick = $derived(prQuickAction(pr));
	const mergeCheck = $derived(mergeAvailability(pr));
	const ref = $derived(`${pr.repo}#${pr.number}`);

	const RUNNING_LABEL: Record<PrAction, string> = {
		ready: 'Marking ready…',
		draft: 'Converting to draft…',
		reviewers: 'Requesting reviewers…',
		comment: 'Posting comment…',
		update_branch: 'Updating branch…',
		merge: 'Merging…'
	};

	async function run(action: PrAction, extra: Partial<PrActionRequest> = {}) {
		if (running) return;
		panel = null;
		running = action;
		error = null;
		done = null;
		clearTimeout(doneTimer);
		try {
			const res = await engineApi.runPrAction({ repo: pr.repo, number: pr.number, action, ...extra });
			done = res.note ?? prDoneMessage(action, res);
			if (action === 'comment') comment = '';
			if (action === 'reviewers') reviewers = '';
			merging = null;
			// A merged PR leaves the list; keep the confirmation visible first.
			if (res.removed) setTimeout(() => onresult(res), 1500);
			else onresult(res);
			doneTimer = setTimeout(() => (done = null), 4000);
		} catch (e: any) {
			const message = e?.message ?? 'The action failed';
			if (action === 'merge' && merging) merging.error = message;
			else {
				error = message;
				panel = 'error';
			}
		} finally {
			running = null;
		}
	}

	function openMerge() {
		panel = null;
		const method = defaultMergeMethod(pr);
		if (!mergeCheck.ok || !method || !pr.head_sha) return;
		merging = { sha: pr.head_sha, method, deleteBranch: pr.delete_branch_on_merge ?? false, error: null };
	}

	function confirmMerge() {
		if (!merging) return;
		run('merge', { method: merging.method, head_sha: merging.sha, delete_branch: merging.deleteBranch });
	}

	const item =
		'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-surface-200 transition-colors hover:bg-surface-800 disabled:opacity-40 disabled:hover:bg-transparent';
	const CI_TEXT: Record<string, string> = {
		green: 'text-emerald-300',
		red: 'text-rose-300',
		amber: 'text-amber-300',
		none: 'text-surface-400'
	};
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
		<button type="button" class="rounded px-1 text-laya-micro text-rose-300 hover:bg-rose-500/10" onclick={() => (panel = 'error')} title={error}>Failed</button>
	{:else if quick === 'ready'}
		<button
			type="button"
			onclick={() => run('ready')}
			class="rounded border border-surface-700 px-1.5 py-px text-laya-micro text-surface-300 transition-colors hover:border-surface-500 hover:text-surface-100"
			>Ready for review</button
		>
	{:else if quick === 'merge'}
		<button
			type="button"
			onclick={openMerge}
			class="rounded border border-emerald-500/40 px-1.5 py-px text-laya-micro text-emerald-300 transition-colors hover:bg-emerald-500/10"
			>Merge</button
		>
	{/if}
	<button
		type="button"
		bind:this={menuBtn}
		onclick={() => (panel = panel ? null : 'menu')}
		disabled={!!running}
		class="flex h-5 w-5 items-center justify-center rounded text-surface-500 transition-colors hover:bg-surface-700 hover:text-surface-100 disabled:opacity-50"
		title="Actions"
		aria-label="Actions for {ref}"
		aria-haspopup="menu"
	>
		<svg viewBox="0 0 16 16" class="h-3.5 w-3.5" fill="currentColor"><circle cx="3.5" cy="8" r="1.3" /><circle cx="8" cy="8" r="1.3" /><circle cx="12.5" cy="8" r="1.3" /></svg>
	</button>
</div>

{#if panel === 'menu'}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Actions for {ref}" width={210}>
		<div role="menu">
			{#if pr.draft}
				<button type="button" role="menuitem" class={item} onclick={() => run('ready')}>Ready for review</button>
			{:else}
				<button type="button" role="menuitem" class={item} onclick={() => run('draft')}>Convert to draft</button>
			{/if}
			<button type="button" role="menuitem" class={item} onclick={() => (panel = 'reviewers')}>Request reviewers…</button>
			<button type="button" role="menuitem" class={item} onclick={() => (panel = 'comment')}>Comment…</button>
			<button type="button" role="menuitem" class={item} onclick={() => run('update_branch')}>
				<span class="flex-1">Update branch</span>
				{#if pr.merge_state === 'BEHIND'}<span class="text-laya-micro text-amber-300">behind</span>{/if}
			</button>
			<button type="button" role="menuitem" class={item} disabled={!mergeCheck.ok} title={mergeCheck.reason ?? ''} onclick={openMerge}>
				<span class="flex-1">Merge…</span>
				{#if mergeCheck.reason}<span class="truncate text-laya-micro text-surface-500">{mergeCheck.reason}</span>{/if}
			</button>
		</div>
	</RowPanel>
{:else if panel === 'reviewers'}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Request reviewers for {ref}" width={300}>
		<form
			class="p-1.5"
			onsubmit={(e) => {
				e.preventDefault();
				const list = parseReviewers(reviewers);
				if (list.length) run('reviewers', { reviewers: list });
			}}
		>
			<label class="mb-1 block text-laya-micro text-surface-400" for="rev-{pr.number}">GitHub logins or org/team, comma separated</label>
			<!-- svelte-ignore a11y_autofocus -->
			<input
				id="rev-{pr.number}"
				bind:value={reviewers}
				autofocus
				placeholder="octocat, acme/core"
				class="w-full rounded border border-surface-700 bg-surface-950 px-2 py-1 text-laya-secondary text-surface-100 focus:border-surface-500 focus:outline-none"
			/>
			<div class="mt-1.5 flex justify-end gap-2">
				<button type="button" class="rounded px-2 py-0.5 text-laya-micro text-surface-400 hover:text-surface-100" onclick={() => (panel = null)}>Cancel</button>
				<button type="submit" class="rounded bg-laya-orange/90 px-2.5 py-0.5 text-laya-micro font-medium text-surface-950 hover:bg-laya-orange disabled:opacity-40" disabled={!parseReviewers(reviewers).length}>Request</button>
			</div>
		</form>
	</RowPanel>
{:else if panel === 'comment'}
	<RowPanel anchor={menuBtn} onclose={() => (panel = null)} label="Comment on {ref}" width={320}>
		<div class="p-1.5">
			<p class="mb-1 text-laya-micro text-surface-400">Comment on {ref} (Markdown)</p>
			<!-- svelte-ignore a11y_autofocus -->
			<textarea
				bind:value={comment}
				autofocus
				rows="4"
				class="w-full resize-y rounded border border-surface-700 bg-surface-950 px-2 py-1 text-laya-secondary text-surface-100 focus:border-surface-500 focus:outline-none"
				onkeydown={(e) => {
					if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && comment.trim()) run('comment', { body: comment });
				}}
			></textarea>
			<div class="mt-1.5 flex justify-end gap-2">
				<button type="button" class="rounded px-2 py-0.5 text-laya-micro text-surface-400 hover:text-surface-100" onclick={() => (panel = null)}>Cancel</button>
				<button type="button" class="rounded bg-laya-orange/90 px-2.5 py-0.5 text-laya-micro font-medium text-surface-950 hover:bg-laya-orange disabled:opacity-40" disabled={!comment.trim()} onclick={() => run('comment', { body: comment })}>Post comment</button>
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

{#if merging}
	{@const ci = ciStatus(pr.checks)}
	{@const review = reviewState(pr)}
	<div use:portal class="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 p-4" role="presentation" onmousedown={(e) => { if (e.target === e.currentTarget && !running) merging = null; }}>
		<div
			role="dialog"
			aria-modal="true"
			aria-labelledby="merge-title-{pr.number}"
			tabindex="-1"
			class="w-full max-w-md rounded-xl border border-surface-700 bg-surface-900 p-4 text-laya-secondary shadow-2xl"
			onkeydown={(e) => { if (e.key === 'Escape' && !running) merging = null; }}
		>
			<h2 id="merge-title-{pr.number}" class="text-sm font-semibold text-surface-100">Merge pull request</h2>
			<p class="mt-1 font-mono text-laya-micro text-surface-400">{ref}</p>
			<p class="mt-0.5 text-surface-200">{pr.title}</p>

			<dl class="mt-3 grid grid-cols-[7rem_1fr] gap-x-2 gap-y-1 text-laya-micro">
				<dt class="text-surface-500">Head commit</dt>
				<dd class="font-mono text-surface-200" title={merging.sha}>{shortSha(merging.sha)} <span class="font-sans text-surface-500">({pr.branch} → {pr.base})</span></dd>
				<dt class="text-surface-500">Checks</dt>
				<dd class={CI_TEXT[ci.color]}>{ci.label}</dd>
				<dt class="text-surface-500">Review</dt>
				<dd class="text-surface-200">{review.label}{pr.unresolved_threads ? ` · ${pr.unresolved_threads} unresolved` : ''}</dd>
				<dt class="text-surface-500">Merge state</dt>
				<dd class="text-surface-200">{mergeStateLabel(pr.merge_state)}</dd>
			</dl>

			<fieldset class="mt-3">
				<legend class="mb-1 text-laya-micro text-surface-500">Method</legend>
				{#each pr.merge_methods ?? [] as m (m)}
					<label class="flex items-center gap-2 py-0.5 text-surface-200">
						<input type="radio" name="merge-method-{pr.number}" value={m} bind:group={merging.method} disabled={!!running} />
						{MERGE_METHOD_LABELS[m]}
					</label>
				{/each}
			</fieldset>

			<label class="mt-2 flex items-center gap-2 text-surface-200">
				<input type="checkbox" bind:checked={merging.deleteBranch} disabled={!!running} />
				Delete the branch <span class="font-mono text-laya-micro text-surface-400">{pr.branch}</span> after merging
			</label>

			{#if merging.error}
				<p class="mt-3 whitespace-pre-wrap break-words rounded-md bg-rose-500/10 px-2.5 py-1.5 text-laya-micro text-rose-300">{merging.error}</p>
			{/if}

			<p class="mt-3 text-laya-micro text-surface-500">
				GitHub merges only if the head is still {shortSha(merging.sha)}. No admin override, no auto-merge.
			</p>

			<div class="mt-3 flex justify-end gap-2">
				<button type="button" class="rounded px-3 py-1 text-laya-micro text-surface-300 hover:text-surface-100 disabled:opacity-40" disabled={!!running} onclick={() => (merging = null)}>Cancel</button>
				<button
					type="button"
					class="flex items-center gap-1.5 rounded bg-emerald-500/90 px-3 py-1 text-laya-micro font-semibold text-surface-950 hover:bg-emerald-400 disabled:opacity-50"
					disabled={!!running}
					onclick={confirmMerge}
				>
					{#if running === 'merge'}<span class="h-2.5 w-2.5 animate-spin rounded-full border border-surface-950 border-t-transparent"></span>Merging…{:else}{MERGE_METHOD_LABELS[merging.method]}{/if}
				</button>
			</div>
		</div>
	</div>
{/if}
