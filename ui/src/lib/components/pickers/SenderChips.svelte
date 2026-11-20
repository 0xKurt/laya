<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	The Gmail source's skipped senders as chips, with the senders seen in recent
	Gmail events (GET /claude-code/gmail/senders) offered as one-click additions.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import ChipInput from './ChipInput.svelte';
	import { getGmailSenders, type GmailSender } from '$lib/api/workScope';
	import type { ChipSuggestion } from '$lib/utils/pickers';

	let { values = $bindable([]), id }: { values?: string[]; id: string } = $props();

	let seen = $state<GmailSender[]>([]);
	let loadError = $state<string | null>(null);

	onMount(async () => {
		try {
			seen = (await getGmailSenders(30, 12)).senders;
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Could not read recent senders';
		}
	});

	const suggestions = $derived<ChipSuggestion[]>(
		seen.map((s) => ({
			value: s.email,
			label: `${s.name || s.email} · ${s.count}`,
			hint: `Skip ${s.email} (${s.count} mail${s.count === 1 ? '' : 's'} in the last 30 days)`
		}))
	);
</script>

<ChipInput
	bind:values
	{id}
	label="Add a sender to skip"
	placeholder="name@example.com, then Enter"
	{suggestions}
	suggestionsLabel="Frequent senders in the last 30 days (click to skip)"
	emptyText="No sender skipped: every mail in your inbox is read."
/>
{#if loadError}<p class="text-xs text-surface-500">Suggestions unavailable: {loadError}</p>{/if}
