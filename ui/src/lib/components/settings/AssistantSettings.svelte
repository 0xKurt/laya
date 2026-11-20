<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Settings → Agent: the assistant's working rules (settings `assistant.rules`,
	empty = the neutral default from GET /claude-code/assistant/rules) and the
	folders where repository checkouts live (settings `sessions.checkout_roots`,
	used by "Prepare session").
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { engineApi, engineFetch } from '$lib/api/engine';
	import { glassTheme } from '$lib/stores/glassTheme';

	let rules = $state('');
	let defaultRules = $state('');
	let custom = $state(false);
	let roots = $state('');
	let loading = $state(true);
	let saved = $state<string | null>(null);
	let error = $state<string | null>(null);

	const sectionClass = $derived(
		$glassTheme ? 'glass-section' : 'rounded-xl border border-surface-700 bg-surface-800'
	);

	onMount(async () => {
		try {
			const [resp, settings] = await Promise.all([
				engineFetch('/claude-code/assistant/rules').then((r) => r.json()),
				engineApi.getSettings()
			]);
			rules = resp.rules ?? '';
			defaultRules = resp.default ?? '';
			custom = !!resp.custom;
			const configured = (settings as unknown as { sessions?: { checkout_roots?: string[] } }).sessions
				?.checkout_roots;
			roots = Array.isArray(configured) ? configured.join('\n') : '';
		} catch {
			error = 'Could not load the assistant settings';
		} finally {
			loading = false;
		}
	});

	function flash(what: string) {
		saved = what;
		setTimeout(() => (saved = null), 1500);
	}

	async function saveRules(text: string) {
		error = null;
		try {
			const value = text.trim() === defaultRules.trim() ? '' : text.trim();
			await engineApi.updateSettings({ assistant: { rules: value } } as never);
			custom = !!value;
			rules = value || defaultRules;
			flash('rules');
		} catch {
			error = 'Could not save the rules';
		}
	}

	async function saveRoots() {
		error = null;
		const list = roots
			.split(/[\n,]/)
			.map((r) => r.trim())
			.filter(Boolean);
		try {
			await engineApi.updateSettings({ sessions: { checkout_roots: list } } as never);
			flash('roots');
		} catch {
			error = 'Could not save the checkout folders';
		}
	}
</script>

<div class="mt-4 space-y-5 {sectionClass} p-4" id="assistant-settings">
	<div>
		<div class="mb-1 flex items-center justify-between">
			<label for="assistant-rules" class="text-laya-base font-medium text-surface-200">Assistant rules</label>
			<span class="text-laya-micro text-surface-500">
				{saved === 'rules' ? 'Saved' : custom ? 'your rules' : 'default rules'}
			</span>
		</div>
		<p class="mb-2 text-laya-secondary text-surface-400">
			How the assistant works for you: scope (orgs, workspaces), what it may never do, how it answers. Sent to the
			assistant with Laya's tools in every session.
		</p>
		<textarea
			id="assistant-rules"
			rows="8"
			class="w-full rounded-md border border-surface-600 bg-surface-900 px-3 py-2 font-mono text-laya-secondary text-surface-200 focus:border-laya-orange focus:outline-none"
			bind:value={rules}
			disabled={loading}
		></textarea>
		<div class="mt-2 flex gap-2">
			<button
				class="rounded-md bg-laya-orange px-3 py-1.5 text-laya-secondary font-medium text-white disabled:opacity-50"
				onclick={() => saveRules(rules)}
				disabled={loading}>Save rules</button
			>
			<button
				class="rounded-md border border-surface-600 px-3 py-1.5 text-laya-secondary text-surface-300 disabled:opacity-50"
				onclick={() => saveRules(defaultRules)}
				disabled={loading || !custom}>Reset to default</button
			>
		</div>
	</div>

	<div>
		<label for="checkout-roots" class="mb-1 block text-laya-base font-medium text-surface-200">
			Repository checkout folders
		</label>
		<p class="mb-2 text-laya-secondary text-surface-400">
			{#if roots.trim()}
				"Prepare session" looks for a card's repository in these folders (one per line).
			{:else}
				Not set: "Prepare session" looks in your home folder. Enter the folder(s) where your repositories are checked
				out, one per line.
			{/if}
		</p>
		<textarea
			id="checkout-roots"
			rows="2"
			placeholder="~/code"
			class="w-full rounded-md border border-surface-600 bg-surface-900 px-3 py-2 font-mono text-laya-secondary text-surface-200 focus:border-laya-orange focus:outline-none"
			bind:value={roots}
			onchange={saveRoots}
			disabled={loading}
		></textarea>
		{#if saved === 'roots'}<p class="mt-1 text-laya-micro text-green-400/80">Saved</p>{/if}
	</div>
	{#if error}<p class="text-laya-secondary text-error-400">{error}</p>{/if}
</div>
