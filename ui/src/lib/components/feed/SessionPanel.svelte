<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	// Effort estimate and "Prepare session" of a card (docs/claude-code-connector.md,
	// "Effort and session lane"). The prepared prompt and worktree commands are
	// only shown for copying: the user starts the session himself.
	import type { ActionCard, Effort, SessionPrep } from '$lib/api/types';
	import { untrack } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import { effortOf, formatEffort, formatMinutes, parseDuration } from '$lib/sessions/session';

	let { card, prepOpen = $bindable(false) }: { card: ActionCard; prepOpen?: boolean } = $props();

	// ── effort ──
	let override = $state<Effort | null>(null);
	const effort = $derived(override ?? effortOf(card));
	let editing = $state(false);
	let youInput = $state('');
	let agentInput = $state('');
	let saving = $state(false);
	let effortError = $state<string | null>(null);

	// ── session prep ──
	let prep = $state<SessionPrep | null>(null);
	let preparing = $state(false);
	let prepError = $state<string | null>(null);
	let copiedKey = $state<string | null>(null);

	$effect(() => {
		card.card_id;
		override = null;
		editing = false;
		prep = null;
		prepError = null;
		prepOpen = false;
	});

	$effect(() => {
		if (prepOpen && !prep && !preparing) untrack(() => prepare());
	});

	function startEdit() {
		youInput = effort ? formatMinutes(effort.you_minutes) : '';
		agentInput = effort ? formatMinutes(effort.agent_minutes) : '';
		effortError = null;
		editing = true;
	}

	async function saveEffort() {
		const you = parseDuration(youInput);
		const agent = parseDuration(agentInput);
		if (you === null || agent === null) {
			effortError = 'Use minutes or hours, e.g. 30m, 2h, 1h30m';
			return;
		}
		saving = true;
		try {
			const res = await engineApi.updateEffort(card.card_id, {
				you_minutes: you,
				agent_minutes: agent,
				basis: effort?.basis ?? ''
			});
			override = res.effort;
			editing = false;
		} catch (e: any) {
			effortError = e?.message ?? 'Could not save';
		} finally {
			saving = false;
		}
	}

	async function prepare() {
		const cardId = card.card_id;
		preparing = true;
		prepError = null;
		try {
			const res = await engineApi.prepareSession(cardId);
			if (card.card_id === cardId) prep = res;
		} catch (e: any) {
			if (card.card_id === cardId) prepError = e?.message ?? 'Could not prepare the session';
		} finally {
			preparing = false;
		}
	}

	async function copy(key: string, text: string) {
		try {
			await navigator.clipboard.writeText(text);
			copiedKey = key;
			setTimeout(() => {
				if (copiedKey === key) copiedKey = null;
			}, 1500);
		} catch {
			// clipboard unavailable: the text stays selectable in the panel
		}
	}
</script>

<div class="mb-5 flex flex-col gap-2">
	<div class="flex flex-wrap items-center gap-2 text-laya-secondary">
		{#if editing}
			<label class="flex items-center gap-1 text-surface-400">
				you
				<input bind:value={youInput} class="w-16 rounded border border-surface-700 bg-surface-900/60 px-1.5 py-0.5 text-surface-100" />
			</label>
			<label class="flex items-center gap-1 text-surface-400">
				agent
				<input bind:value={agentInput} class="w-16 rounded border border-surface-700 bg-surface-900/60 px-1.5 py-0.5 text-surface-100" />
			</label>
			<button type="button" onclick={saveEffort} disabled={saving} class="rounded-md bg-laya-orange/15 px-2 py-0.5 text-laya-orange hover:bg-laya-orange/25 disabled:opacity-50">
				{saving ? 'Saving…' : 'Save'}
			</button>
			<button type="button" onclick={() => (editing = false)} class="text-surface-500 hover:text-surface-200">Cancel</button>
		{:else}
			<span class="text-surface-300" title={effort?.basis ?? ''}>
				{effort ? formatEffort(effort) : 'No effort estimate'}
			</span>
			{#if effort?.set_by === 'user'}
				<span class="text-laya-micro text-surface-500">(yours)</span>
			{/if}
			<button type="button" onclick={startEdit} class="text-laya-micro text-surface-500 underline-offset-2 hover:text-surface-200 hover:underline">
				{effort ? 'Correct' : 'Estimate'}
			</button>
		{/if}
		<span class="flex-1"></span>
		<button
			type="button"
			onclick={() => (prepOpen = !prepOpen)}
			class="rounded-md border border-surface-700 px-2 py-0.5 text-laya-micro font-medium text-surface-300 transition-colors hover:border-laya-orange/60 hover:text-laya-orange"
		>
			{prepOpen ? 'Hide session' : 'Prepare session'}
		</button>
	</div>
	{#if effortError}
		<p class="text-laya-micro text-rose-300">{effortError}</p>
	{/if}

	{#if prepOpen}
		<div class="flex flex-col gap-3 rounded-lg border border-surface-700 bg-surface-900/50 p-3">
			{#if preparing}
				<p class="text-laya-secondary text-surface-500">Writing the session prompt…</p>
			{:else if prepError}
				<p class="text-laya-secondary text-rose-300">{prepError}</p>
				<button type="button" onclick={prepare} class="w-fit text-laya-micro text-surface-400 hover:text-surface-200">Try again</button>
			{:else if prep}
				<div>
					<div class="mb-1 flex items-center gap-2">
						<h4 class="text-laya-micro font-semibold uppercase tracking-wider text-surface-400">Session prompt</h4>
						<span class="flex-1"></span>
						<button type="button" onclick={() => copy('prompt', prep!.prompt)} class="text-laya-micro text-surface-400 hover:text-laya-orange">
							{copiedKey === 'prompt' ? 'Copied' : 'Copy'}
						</button>
					</div>
					<pre class="whitespace-pre-wrap break-words rounded bg-surface-950/60 p-2 text-laya-secondary text-surface-200">{prep.prompt}</pre>
					{#if prep.llm_error}
						<p class="mt-1 text-laya-micro text-amber-200/90">Written from the card without the model ({prep.llm_error}).</p>
					{/if}
				</div>
				{#if prep.commands.length > 0}
					<div>
						<div class="mb-1 flex items-center gap-2">
							<h4 class="text-laya-micro font-semibold uppercase tracking-wider text-surface-400">Worktree</h4>
							<span class="truncate text-laya-micro text-surface-500">{prep.repo?.full_name ?? prep.repo?.name} · {prep.repo?.default_branch}</span>
							<span class="flex-1"></span>
							<button type="button" onclick={() => copy('commands', prep!.commands.join('\n'))} class="text-laya-micro text-surface-400 hover:text-laya-orange">
								{copiedKey === 'commands' ? 'Copied' : 'Copy'}
							</button>
						</div>
						<pre class="overflow-x-auto rounded bg-surface-950/60 p-2 text-laya-micro text-surface-200">{prep.commands.join('\n')}</pre>
					</div>
				{/if}
				{#each prep.notes as note (note)}
					<p class="text-laya-micro text-surface-500">{note}</p>
				{/each}
				<p class="text-laya-micro text-surface-500">Nothing is started. Paste the commands in a terminal and start the session there.</p>
			{/if}
		</div>
	{/if}
</div>
