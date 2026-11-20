<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { referenceInClaude } from '$lib/stores/claudeTerminal';

	/**
	 * Icon button that inserts a one-line reference into the active Claude
	 * terminal tab. `reference` is built lazily so rows do not compute it until
	 * the button is clicked.
	 */
	let {
		reference,
		class: className = '',
		size = 'h-3 w-3'
	}: { reference: () => string; class?: string; size?: string } = $props();

	let busy = $state(false);

	async function onclick(e: MouseEvent) {
		e.preventDefault();
		e.stopPropagation();
		if (busy) return;
		busy = true;
		try {
			await referenceInClaude(reference());
		} finally {
			busy = false;
		}
	}
</script>

<button
	type="button"
	{onclick}
	onkeydown={(e) => e.stopPropagation()}
	disabled={busy}
	aria-label="Send to Assistant"
	title="Send to Assistant"
	class="flex items-center justify-center rounded-md transition-all disabled:opacity-40 {className}"
>
	<svg class={size} fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
		<path stroke-linecap="round" stroke-linejoin="round" d="M4 17l6-5-6-5M12 19h8" />
	</svg>
</button>
