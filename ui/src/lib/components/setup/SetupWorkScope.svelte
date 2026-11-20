<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Setup wizard: the work scope. Sets the GitHub source's `orgs` (the same config
	Settings → Integrations → GitHub edits, read by collecting and My work) from
	the organizations `gh` reports; none checked = every repository. Each change
	is saved right away.
-->
<script lang="ts">
	import OrgPicker from '$lib/components/pickers/OrgPicker.svelte';
	import { setGithubOrgs, type GithubOrgs } from '$lib/api/workScope';

	let selected = $state<string[]>([]);
	let connected = $state(true);
	let error = $state<string | null>(null);
	let status = $state<'idle' | 'saving' | 'saved'>('idle');
	let savedTimer: ReturnType<typeof setTimeout> | undefined;

	function loaded(info: GithubOrgs) {
		selected = [...info.selected];
		connected = info.connected;
	}

	async function save(next: string[]) {
		status = 'saving';
		error = null;
		try {
			await setGithubOrgs(next);
			status = 'saved';
			clearTimeout(savedTimer);
			savedTimer = setTimeout(() => (status = 'idle'), 1500);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not save the scope';
			status = 'idle';
		}
	}
</script>

<div class="space-y-2 rounded-md border border-surface-600 bg-surface-700 p-4" data-testid="setup-work-scope">
	<div class="flex items-baseline justify-between">
		<div class="text-sm font-medium">Work scope</div>
		{#if status === 'saving'}<span class="text-xs text-surface-400">Saving…</span>{/if}
		{#if status === 'saved'}<span class="text-xs text-green-400/80">Saved</span>{/if}
	</div>
	<p class="text-xs text-surface-400">
		The GitHub organizations Laya reads (pull requests, issues, My work). Check the ones that are your work.
	</p>
	<OrgPicker bind:selected disabled={!connected} onload={loaded} onchange={save} />
	{#if !connected}
		<p class="text-xs text-surface-400">Connect GitHub above to set the scope.</p>
	{/if}
	{#if error}<p class="text-xs text-red-400" role="alert">{error}</p>{/if}
</div>
