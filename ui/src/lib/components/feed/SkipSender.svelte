<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	"Skip this sender" on a Gmail card: adds the sender to the Gmail source's
	skipped senders (the same config as Settings → Integrations → Gmail → Skip
	mail from), confirms, and offers Undo. Shown only while Gmail is connected
	through the coding agent.
-->
<script lang="ts">
	import type { ActionCard } from '$lib/api/types';
	import { getGmailSkipped, setGmailSenderSkipped } from '$lib/api/workScope';
	import { normalizeEmail } from '$lib/utils/pickers';

	let { card }: { card: ActionCard } = $props();

	const isGmail = $derived((card.entity_id ?? '').startsWith('gmail:'));
	const sender = $derived(normalizeEmail(card.actor_email ?? ''));

	let connected = $state(false);
	let skipped = $state(false);
	let justSkipped = $state(false);
	let busy = $state(false);
	let error = $state<string | null>(null);

	$effect(() => {
		const email = sender;
		connected = false;
		skipped = false;
		justSkipped = false;
		error = null;
		if (!isGmail || !email) return;
		getGmailSkipped()
			.then((r) => {
				if (email !== sender) return;
				connected = r.connected;
				skipped = r.skipped.includes(email);
			})
			.catch(() => (connected = false));
	});

	async function set(skip: boolean) {
		busy = true;
		error = null;
		try {
			const list = await setGmailSenderSkipped(sender, skip);
			skipped = list.includes(sender);
			justSkipped = skip;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not change the skipped senders';
		} finally {
			busy = false;
		}
	}
</script>

{#if isGmail && sender && connected}
	<div class="mt-1 flex flex-wrap items-center gap-2 text-laya-micro" data-testid="skip-sender">
		{#if !skipped}
			<button
				type="button"
				disabled={busy}
				onclick={() => set(true)}
				title="Stop reading mail from {sender}"
				class="rounded-md border border-surface-700 px-2 py-0.5 text-surface-300 transition-colors hover:border-laya-orange/60 hover:text-laya-orange disabled:opacity-50"
				>{busy ? 'Saving…' : 'Skip this sender'}</button
			>
		{:else}
			<span class="text-surface-300" role="status">
				{justSkipped ? `Skipped: mail from ${sender} is not read from the next sync on.` : `Mail from ${sender} is skipped.`}
			</span>
			<button
				type="button"
				disabled={busy}
				onclick={() => set(false)}
				class="font-medium text-surface-200 underline underline-offset-2 hover:text-laya-orange disabled:opacity-50"
				>{justSkipped ? 'Undo' : 'Stop skipping'}</button
			>
		{/if}
		{#if error}<span class="text-rose-300" role="alert">{error}</span>{/if}
	</div>
{/if}
