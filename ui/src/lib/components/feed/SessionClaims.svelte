<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	// "Merged" / "green" in a card's text is a session's report until gh
	// confirms it. The engine checks each claim with gh; only confirmed ones
	// read as facts here.
	import type { ActionCard, SessionClaim } from '$lib/api/types';
	import { untrack } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import { cardClaimText, claimLines, mentionsClaim } from '$lib/sessions/session';

	let { card }: { card: ActionCard } = $props();

	let claims = $state<SessionClaim[]>([]);
	let checking = $state(false);
	let failed = $state<string | null>(null);

	const text = $derived(cardClaimText(card));

	async function check(cardId: string) {
		checking = true;
		failed = null;
		try {
			const res = await engineApi.getSessionClaims(cardId);
			if (card.card_id === cardId) claims = res.claims;
		} catch (e: any) {
			if (card.card_id === cardId) failed = e?.message ?? 'Could not check';
		} finally {
			checking = false;
		}
	}

	$effect(() => {
		const cardId = card.card_id;
		claims = [];
		failed = null;
		if (mentionsClaim(text)) untrack(() => check(cardId));
	});

	const TONE_CLASS = {
		fact: 'text-emerald-300',
		conflict: 'text-rose-300',
		lead: 'text-amber-200/90'
	} as const;
</script>

{#if checking || claims.length > 0 || failed}
	<div class="mb-5">
		<h3 class="mb-2 text-laya-secondary font-semibold uppercase tracking-wider text-surface-400">Session claims</h3>
		{#if checking}
			<p class="text-laya-secondary text-surface-500">Checking with gh…</p>
		{:else if failed}
			<p class="text-laya-secondary text-amber-200/90">Reported by the session, unverified ({failed})</p>
		{:else}
			<ul class="flex flex-col gap-1">
				{#each claims as claim, i (i)}
					{#each claimLines(claim) as line (line.text)}
						<li class="flex items-start gap-1.5 text-laya-secondary {TONE_CLASS[line.tone]}">
							<span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-current"></span>
							{#if claim.url}
								<a href={claim.url} target="_blank" rel="noopener noreferrer" class="hover:underline">{line.text}</a>
							{:else}
								<span title={claim.error ?? ''}>{line.text}</span>
							{/if}
						</li>
					{/each}
				{/each}
			</ul>
		{/if}
	</div>
{/if}
