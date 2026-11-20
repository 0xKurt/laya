<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Setup wizard: what Laya's own processing can run on. Lists the coding agents
	found with their logins (GET /claude-code/accounts) and the API keys saved in
	this step, and offers "Automatic" (every role on whichever subscription login
	has room) when a login exists.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import type { ClaudeAccountsOverview } from '$lib/api/types';

	let {
		apiKeys = [],
		useAutomatic = $bindable(true)
	}: {
		apiKeys?: { provider: string; label: string }[];
		useAutomatic?: boolean;
	} = $props();

	let overview = $state<ClaudeAccountsOverview | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let touched = false;

	const agents = $derived(overview?.agents ?? []);
	const loginsOf = (agentId: string) =>
		(overview?.accounts ?? []).filter((a) => (a.agent_id ?? 'claude_code') === agentId && a.logged_in);
	const anyLogin = $derived(agents.some((g) => g.available && loginsOf(g.agent_id).length > 0));

	onMount(async () => {
		try {
			overview = await engineApi.getClaudeAccounts();
			if (!touched) useAutomatic = anyLogin;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not look for coding agents';
		} finally {
			loading = false;
		}
	});
</script>

<div class="space-y-3 rounded-md border border-surface-600 bg-surface-800 p-4" data-testid="setup-model-access">
	<div class="text-sm font-medium">Found on this computer</div>
	{#if loading}
		<p class="text-xs text-surface-400">Looking for coding agents and their logins…</p>
	{:else if error}
		<p class="text-xs text-red-400">{error}</p>
	{:else}
		<ul class="space-y-1.5 text-sm">
			{#each agents as g (g.agent_id)}
				{@const logins = loginsOf(g.agent_id)}
				<li class="flex flex-wrap items-baseline gap-2">
					<span class="h-1.5 w-1.5 rounded-full {g.available && logins.length ? 'bg-green-500' : 'bg-surface-500'}"></span>
					<span class="text-surface-100">{g.label}</span>
					<span class="text-xs text-surface-400">
						{#if !g.available}
							not installed
						{:else if logins.length === 0}
							installed, not logged in
						{:else}
							{logins.map((l) => [l.email ?? 'logged in', l.plan].filter(Boolean).join(' · ')).join(', ')}
						{/if}
					</span>
				</li>
			{/each}
			<li class="flex flex-wrap items-baseline gap-2">
				<span class="h-1.5 w-1.5 rounded-full {apiKeys.length ? 'bg-green-500' : 'bg-surface-500'}"></span>
				<span class="text-surface-100">API keys</span>
				<span class="text-xs text-surface-400">
					{apiKeys.length ? apiKeys.map((k) => k.label).join(', ') : 'none yet (optional, below)'}
				</span>
			</li>
		</ul>
	{/if}
	<label class="flex cursor-pointer items-start gap-3">
		<input
			type="checkbox"
			class="mt-0.5 accent-laya-orange"
			bind:checked={useAutomatic}
			onchange={() => (touched = true)}
			disabled={!loading && !anyLogin}
		/>
		<span>
			<span class="block text-sm font-medium">Run Laya's models on my subscription logins (Automatic)</span>
			<span class="block text-xs text-surface-400">
				Each call runs on whichever login has room and moves on when one reaches its limit. Add more logins in
				Settings → Models → Access. API keys are used only if you allow it there.
			</span>
		</span>
	</label>
</div>
