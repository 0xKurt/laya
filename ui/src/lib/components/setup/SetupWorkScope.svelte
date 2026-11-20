<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Setup wizard: the work scope. Sets the GitHub source's `orgs` (the same config
	Settings → Integrations → GitHub edits, read by collecting and My work) from
	the organizations `gh` reports; none checked = every repository.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { getGithubOrgs, setGithubOrgs, type GithubOrgs } from '$lib/api/workScope';

	let info = $state<GithubOrgs | null>(null);
	let selected = $state<string[]>([]);
	let error = $state<string | null>(null);
	let saving = $state(false);

	async function load() {
		try {
			info = await getGithubOrgs();
			selected = [...info.selected];
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not read your GitHub organizations';
		}
	}

	onMount(load);

	async function toggle(org: string) {
		selected = selected.includes(org) ? selected.filter((o) => o !== org) : [...selected, org];
		saving = true;
		error = null;
		try {
			await setGithubOrgs(selected);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not save the scope';
		} finally {
			saving = false;
		}
	}
</script>

<div class="space-y-2 rounded-md border border-surface-600 bg-surface-700 p-4" data-testid="setup-work-scope">
	<div class="flex items-baseline justify-between">
		<div class="text-sm font-medium">Work scope</div>
		{#if saving}<span class="text-xs text-surface-400">Saving…</span>{/if}
	</div>
	<p class="text-xs text-surface-400">
		The GitHub organizations Laya reads (pull requests, issues, My work). Check the ones that are your work.
	</p>
	{#if !info && !error}
		<p class="text-xs text-surface-400">Reading your organizations from GitHub…</p>
	{:else if info}
		{#if info.orgs.length === 0}
			<p class="text-xs text-surface-400">{info.error ?? 'Your GitHub login belongs to no organization.'}</p>
		{/if}
		<div class="flex flex-wrap gap-2">
			{#each info.orgs as org (org)}
				<label class="flex cursor-pointer items-center gap-2 rounded-md border border-surface-600 bg-surface-800 px-3 py-1.5 text-sm">
					<input
						type="checkbox"
						class="accent-laya-orange"
						checked={selected.includes(org)}
						onchange={() => toggle(org)}
						disabled={!info.connected}
					/>
					{org}
				</label>
			{/each}
		</div>
		{#if !info.connected}
			<p class="text-xs text-surface-400">Connect GitHub above to set the scope.</p>
		{:else if selected.length === 0}
			<p class="text-xs text-amber-300">
				No organization checked: Laya reads every repository your GitHub login can see, including personal and other
				organizations' repositories.
			</p>
		{/if}
	{/if}
	{#if error}<p class="text-xs text-red-400">{error}</p>{/if}
</div>
