<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	The model of an installed coding agent: its default, the models the agent
	offers (GET /claude-code/agent-models), or "Custom…" to type a model name.
	`value` is the plain model name ('' = the agent's default).
-->
<script lang="ts">
	import { agentModelOptions, getAgentModels, type ModelOption } from '$lib/api/modelAccess';

	let {
		id,
		agentId,
		value,
		placeholder = 'Model name (blank = agent default)',
		class: cls = '',
		onchange
	}: {
		id: string;
		agentId: string;
		value: string;
		placeholder?: string;
		class?: string;
		onchange: (model: string) => void;
	} = $props();

	const CUSTOM = '__custom__';
	let known = $state<ModelOption[]>([]);
	let custom = $state(false);

	$effect(() => {
		const agent = agentId;
		getAgentModels()
			.then((m) => (known = m.agents[agent] ?? []))
			.catch(() => (known = []));
	});

	const options = $derived(agentModelOptions(known, value));
</script>

{#if custom}
	<div class="flex items-center gap-2">
		<!-- svelte-ignore a11y_autofocus -->
		<input
			{id}
			type="text"
			autofocus
			{value}
			{placeholder}
			spellcheck="false"
			autocapitalize="off"
			class="{cls} font-mono"
			onchange={(e) => onchange((e.target as HTMLInputElement).value.trim())}
		/>
		<button type="button" class="shrink-0 text-xs text-surface-400 hover:text-surface-200" onclick={() => (custom = false)}>
			List
		</button>
	</div>
{:else}
	<select
		{id}
		{value}
		class={cls}
		onchange={(e) => {
			const v = (e.target as HTMLSelectElement).value;
			if (v === CUSTOM) {
				custom = true;
				return;
			}
			onchange(v);
		}}
	>
		{#each options as o (o.value)}
			<option value={o.value}>{o.label}</option>
		{/each}
		<option value={CUSTOM}>Custom…</option>
	</select>
{/if}
