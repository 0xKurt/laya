<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	The GitHub organizations of the user's `gh` login as a checkbox list
	(GET /claude-code/github/orgs), with "Select all / none" and the warning that
	none checked means every repository. Used by the setup wizard's work scope
	step and Settings → Integrations → GitHub; the caller decides when to save.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { getGithubOrgs, type GithubOrgs } from '$lib/api/workScope';
	import { selectAll, toggleItem, unknownSelected } from '$lib/utils/pickers';

	let {
		selected = $bindable([]),
		disabled = false,
		onchange,
		onload
	}: {
		selected?: string[];
		disabled?: boolean;
		/** Called with the new selection after every change. */
		onchange?: (selected: string[]) => void;
		/** Called once the orgs are read (the wizard takes the stored selection from it). */
		onload?: (info: GithubOrgs) => void;
	} = $props();

	let info = $state<GithubOrgs | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			info = await getGithubOrgs();
			onload?.(info);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not read your GitHub organizations';
		} finally {
			loading = false;
		}
	}

	onMount(load);

	const orgs = $derived(info?.orgs ?? []);
	const missing = $derived(unknownSelected(orgs, selected));
	const shown = $derived([...orgs, ...missing]);

	function set(next: string[]) {
		selected = next;
		onchange?.(next);
	}
</script>

<div class="flex flex-col gap-2" data-testid="org-picker">
	{#if loading}
		<p class="text-xs text-surface-400" role="status">Reading your organizations from GitHub…</p>
	{:else if error}
		<div class="flex flex-wrap items-center gap-2 text-xs text-red-400" role="alert">
			<span>{error}</span>
			<button type="button" class="font-medium text-surface-200 underline underline-offset-2" onclick={load}>Try again</button>
		</div>
	{:else}
		{#if orgs.length === 0}
			<div class="flex flex-wrap items-center gap-2 text-xs text-surface-400">
				<span>{info?.error ?? 'Your GitHub login belongs to no organization.'}</span>
				<button type="button" class="font-medium text-surface-200 underline underline-offset-2" onclick={load}>Check again</button>
			</div>
		{/if}
		{#if shown.length > 0}
			<div class="flex items-center gap-3 text-xs">
				<button
					type="button"
					class="text-surface-300 hover:text-laya-orange disabled:opacity-50"
					disabled={disabled || orgs.every((o) => selected.includes(o))}
					onclick={() => set(selectAll(orgs, selected))}>Select all</button
				>
				<button
					type="button"
					class="text-surface-300 hover:text-laya-orange disabled:opacity-50"
					disabled={disabled || selected.length === 0}
					onclick={() => set([])}>Select none</button
				>
				<span class="text-surface-500">{selected.length} of {shown.length} checked</span>
			</div>
			<div class="flex flex-wrap gap-2">
				{#each shown as org (org)}
					<label
						class="flex cursor-pointer items-center gap-2 rounded-md border border-surface-600 bg-surface-800 px-3 py-1.5 text-sm text-surface-200"
						class:opacity-60={missing.includes(org)}
						title={missing.includes(org) ? 'Your GitHub login no longer lists this organization' : undefined}
					>
						<input
							type="checkbox"
							class="accent-laya-orange"
							checked={selected.includes(org)}
							onchange={() => set(toggleItem(selected, org, shown))}
							{disabled}
						/>
						{org}
					</label>
				{/each}
			</div>
		{/if}
		{#if selected.length === 0 && !disabled}
			<p class="text-xs text-amber-300" role="note">
				No organization checked: Laya reads every repository your GitHub login can see, including personal and other
				organizations' repositories.
			</p>
		{/if}
	{/if}
</div>
