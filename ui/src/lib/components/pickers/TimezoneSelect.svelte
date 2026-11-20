<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	A searchable select of the IANA time zones. The empty value means "this
	computer's time zone" and is the default. Shows the current time in the
	chosen zone.
-->
<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { allTimezones, filterTimezones, systemTimezone, timeIn } from '$lib/utils/pickers';

	let { value = $bindable(''), id }: { value?: string; id: string } = $props();

	const zones = allTimezones();
	const system = systemTimezone();

	let open = $state(false);
	let query = $state('');
	let active = $state(0);
	let now = $state(new Date());
	let root: HTMLElement | undefined = $state();
	let searchEl: HTMLInputElement | undefined = $state();
	let listEl: HTMLElement | undefined = $state();

	const effective = $derived(value || system);
	// The system entry first ("" = follow this computer), then the matches.
	const options = $derived(['', ...filterTimezones(zones, query, 80)]);

	let timer: ReturnType<typeof setInterval> | undefined;
	onMount(() => {
		timer = setInterval(() => (now = new Date()), 30_000);
	});
	onDestroy(() => clearInterval(timer));

	function labelOf(z: string): string {
		return z ? z.replace(/_/g, ' ') : `This computer's time zone (${system.replace(/_/g, ' ')})`;
	}

	async function show() {
		open = true;
		query = '';
		active = Math.max(0, options.indexOf(value));
		await tick();
		searchEl?.focus();
		listEl?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
	}

	function choose(z: string) {
		value = z;
		open = false;
	}

	async function onkeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			e.preventDefault();
			active = Math.min(options.length - 1, Math.max(0, active + (e.key === 'ArrowDown' ? 1 : -1)));
			await tick();
			listEl?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' });
		} else if (e.key === 'Enter') {
			e.preventDefault();
			if (options[active] !== undefined) choose(options[active]);
		} else if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			open = false;
		}
	}
</script>

<svelte:window
	onmousedown={(e) => {
		if (open && root && !root.contains(e.target as Node)) open = false;
	}}
/>

<div class="relative" bind:this={root}>
	<button
		{id}
		type="button"
		aria-haspopup="listbox"
		aria-expanded={open}
		onclick={() => (open ? (open = false) : show())}
		class="flex w-full items-center gap-2 rounded-lg border border-integ-input-border bg-integ-input-bg px-3 py-[7px] text-left text-integ-body text-integ-t1 outline-none focus:border-laya-orange focus:ring-[3px] focus:ring-integ-accent-tint"
	>
		<span class="min-w-0 flex-1 truncate">{labelOf(value)}</span>
		<span class="shrink-0 font-mono text-integ-small text-integ-t4" title="Current time in {effective}">
			{timeIn(effective, now)} now
		</span>
		<svg class="h-3.5 w-3.5 shrink-0 text-integ-t4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
		</svg>
	</button>
	{#if open}
		<div class="absolute z-30 mt-1 w-full overflow-hidden rounded-lg border border-surface-600 bg-surface-800 shadow-xl">
			<input
				bind:this={searchEl}
				bind:value={query}
				oninput={() => (active = query ? 1 : 0)}
				{onkeydown}
				placeholder="Search a city or region…"
				aria-label="Search time zones"
				aria-controls="{id}-list"
				class="w-full border-b border-surface-700 bg-transparent px-3 py-2 text-sm text-surface-100 outline-none placeholder:text-surface-500"
			/>
			<ul id="{id}-list" role="listbox" bind:this={listEl} class="max-h-64 overflow-y-auto py-1">
				{#each options as z, i (z)}
					<li
						role="option"
						aria-selected={z === value}
						data-index={i}
						class="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm {i === active
							? 'bg-surface-700 text-surface-50'
							: 'text-surface-200'}"
						onmouseenter={() => (active = i)}
						onmousedown={(e) => {
							e.preventDefault();
							choose(z);
						}}
					>
						<span class="min-w-0 flex-1 truncate">{labelOf(z)}</span>
						<span class="shrink-0 font-mono text-xs text-surface-500">{timeIn(z || system, now)}</span>
						{#if z === value}<span class="text-laya-orange" aria-hidden="true">✓</span>{/if}
					</li>
				{/each}
				{#if options.length === 1}
					<li class="px-3 py-1.5 text-sm text-surface-500">No time zone matches "{query}".</li>
				{/if}
			</ul>
		</div>
	{/if}
</div>
