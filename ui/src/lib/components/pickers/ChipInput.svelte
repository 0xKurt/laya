<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	A list edited as chips: type a value and press Enter (or paste several) to
	add it after validation, × removes one, suggestions add with one click.
-->
<script lang="ts">
	import { addChips, removeChip, type ChipSuggestion } from '$lib/utils/pickers';

	let {
		values = $bindable([]),
		id,
		label,
		placeholder = '',
		normalize,
		validate,
		suggestions = [],
		suggestionsLabel = 'Suggestions',
		emptyText = 'Nothing added yet.',
		maxSuggestions = 12,
		autofocus = false,
		onchange
	}: {
		values?: string[];
		id: string;
		label: string;
		placeholder?: string;
		normalize?: (v: string) => string;
		validate?: (v: string, existing: string[]) => string | null;
		suggestions?: ChipSuggestion[];
		suggestionsLabel?: string;
		emptyText?: string;
		/** At most this many suggestions show; typing filters them. */
		maxSuggestions?: number;
		autofocus?: boolean;
		onchange?: (values: string[]) => void;
	} = $props();

	let input = $state('');
	let error = $state<string | null>(null);

	const offered = $derived.by(() => {
		const q = input.trim().toLowerCase().replace(/^@/, '');
		return suggestions
			.filter((s) => !values.includes(s.value))
			.filter((s) => !q || s.value.toLowerCase().includes(q) || (s.label ?? '').toLowerCase().includes(q))
			.slice(0, maxSuggestions);
	});

	function set(next: string[]) {
		values = next;
		onchange?.(next);
	}

	function add(text = input) {
		if (!text.trim()) {
			error = null;
			return;
		}
		const r = addChips(values, text, normalize, validate);
		if (r.list.length !== values.length) set(r.list);
		error = r.error;
		input = r.rest;
	}

	function onkeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ',' || e.key === ';') {
			e.preventDefault();
			add();
		} else if (e.key === 'Backspace' && !input && values.length) {
			set(values.slice(0, -1));
		}
	}
</script>

<div class="flex flex-col gap-2">
	<div
		class="flex flex-wrap items-center gap-1.5 rounded-lg border border-surface-600 bg-surface-900/60 px-2 py-1.5 focus-within:border-laya-orange"
	>
		{#each values as v (v)}
			<span class="inline-flex items-center gap-1 rounded-full bg-surface-700 py-0.5 pl-2.5 pr-1 text-xs text-surface-100">
				{v}
				<button
					type="button"
					class="rounded-full px-1 text-surface-400 hover:bg-surface-600 hover:text-surface-100"
					aria-label="Remove {v}"
					title="Remove"
					onclick={() => set(removeChip(values, v))}>×</button
				>
			</span>
		{/each}
		<!-- svelte-ignore a11y_autofocus -->
		<input
			{id}
			{autofocus}
			aria-label={label}
			bind:value={input}
			{onkeydown}
			onblur={() => add()}
			onpaste={(e) => {
				const text = e.clipboardData?.getData('text') ?? '';
				if (/[\s,;]/.test(text.trim())) {
					e.preventDefault();
					add(input + text);
				}
			}}
			placeholder={values.length ? 'Add another…' : placeholder}
			class="min-w-[12rem] flex-1 bg-transparent px-1 py-0.5 text-sm text-surface-100 outline-none placeholder:text-surface-500"
		/>
	</div>
	{#if values.length === 0 && emptyText}
		<p class="text-xs text-surface-500">{emptyText}</p>
	{/if}
	{#if error}<p class="text-xs text-red-400" role="alert">{error}</p>{/if}
	{#if offered.length > 0}
		<div class="flex flex-col gap-1.5">
			<span class="text-xs text-surface-400">{suggestionsLabel}</span>
			<div class="flex flex-wrap gap-1.5">
				{#each offered as s (s.value)}
					<button
						type="button"
						class="inline-flex items-center gap-1 rounded-full border border-dashed border-surface-600 px-2.5 py-0.5 text-xs text-surface-300 hover:border-laya-orange/60 hover:text-laya-orange"
						title={s.hint ?? `Add ${s.value}`}
						onmousedown={(e) => e.preventDefault()}
						onclick={() => {
							set([...values, s.value]);
							input = '';
							error = null;
						}}
					>
						<span aria-hidden="true">+</span>
						<span class="max-w-[16rem] truncate">{s.label ?? s.value}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>
