<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- A select of preset durations; a stored value that is not a preset stays selectable. -->
<script lang="ts">
	import { durationLabel, durationOptions, type DurationUnit } from '$lib/utils/pickers';

	let {
		value = $bindable(0),
		id,
		unit,
		presets,
		class: className = '',
		onchange
	}: {
		value?: number;
		id: string;
		unit: DurationUnit;
		presets: number[];
		class?: string;
		/** Called with the new value after a change. */
		onchange?: (value: number) => void;
	} = $props();

	const options = $derived(durationOptions(presets, Number(value)));
</script>

<select {id} class={className} value={Number(value)} onchange={(e) => {
		value = Number(e.currentTarget.value);
		onchange?.(value);
	}}>
	{#each options as o (o)}
		<option value={o}>{durationLabel(o, unit)}</option>
	{/each}
</select>
