<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { onMount } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import { glassTheme } from '$lib/stores/glassTheme';
	import { CODING_AGENTS, DEFAULT_AGENT_PATHS, AGENT_BINARY_NAMES } from '$lib/config';
	import AssistantSettings from './AssistantSettings.svelte';
	import { isTauri } from '$lib/utils/fileDrop';

	let selected = $state('claude_code');
	let agentPaths = $state<Record<string, string>>({ ...DEFAULT_AGENT_PATHS });
	let loading = $state(true);
	let saving = $state(false);
	let detecting = $state(false);
	let error = $state<string | null>(null);

	const hasAgent = $derived(selected !== 'none');
	// What auto-detection finds now, per agent; the stored path differs only when chosen by hand.
	let detectedPaths = $state<Record<string, string> | null>(null);
	const desktop = isTauri();
	let typingPath = $state(false);
	let typedPath = $state('');
	const customPath = $derived(
		detectedPaths !== null && !!agentPaths[selected] && agentPaths[selected] !== (detectedPaths[selected] || '')
	);

	onMount(async () => {
		try {
			const settings = await engineApi.getSettings();
			selected = settings.coding_agent || 'claude_code';
			agentPaths = settings.agent_paths || { ...DEFAULT_AGENT_PATHS };
			engineApi
				.detectAgentPaths()
				.then((r) => (detectedPaths = r.agent_paths))
				.catch(() => {});
		} catch {
			error = 'Failed to load settings';
		} finally {
			loading = false;
		}
	});

	async function selectAgent(value: string) {
		selected = value;
		saving = true;
		error = null;
		try {
			await engineApi.updateSettings({ coding_agent: value });
		} catch {
			error = 'Failed to save coding agent preference';
		} finally {
			saving = false;
		}
	}

	async function detectPaths() {
		detecting = true;
		error = null;
		try {
			const result = await engineApi.detectAgentPaths();
			detectedPaths = result.agent_paths;
			agentPaths = result.agent_paths;
			await engineApi.updateSettings({ agent_paths: result.agent_paths });
		} catch {
			error = 'Failed to detect agent paths';
		} finally {
			detecting = false;
		}
	}

	/** The native file dialog (desktop app); the browser build types the path instead. */
	async function chooseBinary() {
		error = null;
		if (!desktop) {
			typedPath = agentPaths[selected] || '';
			typingPath = true;
			return;
		}
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			const picked = await invoke<string | null>('plugin:dialog|open', {
				options: { multiple: false, directory: false, title: `Choose the ${AGENT_BINARY_NAMES[selected] || selected} binary` }
			});
			if (picked) await saveAgentPath(selected, picked);
		} catch (e) {
			error = String(e ?? '') || 'Could not open the file dialog';
		}
	}

	async function resetToDetected() {
		typingPath = false;
		await saveAgentPath(selected, detectedPaths?.[selected] || '');
	}

	async function saveAgentPath(agentType: string, path: string) {
		saving = true;
		error = null;
		try {
			const updated = { ...agentPaths, [agentType]: path };
			agentPaths = updated;
			await engineApi.updateSettings({ agent_paths: updated });
		} catch {
			error = 'Failed to save agent path';
		} finally {
			saving = false;
		}
	}
</script>

{#if loading}
	<div class="text-surface-400">Loading agent settings...</div>
{:else}
	{#if error}
		<div class="rounded-lg border border-red-800 bg-red-900/30 px-4 py-2 text-laya-base text-red-300">{error}</div>
	{/if}

	<div class="{$glassTheme ? 'glass-section' : 'rounded-xl border border-surface-700 bg-surface-800'} p-4">
		<div class="mb-1 flex items-center justify-between">
			<h3 class="text-laya-heading font-medium">Coding Agent</h3>
			{#if saving}
				<span class="text-laya-micro text-laya-orange">Saving…</span>
			{/if}
		</div>
		<p class="mb-4 text-laya-secondary text-surface-400">Select which CLI coding agent Laya uses for ENGINEER tasks</p>

		<div class="space-y-2">
			{#each CODING_AGENTS as agent}
				<button
					class="flex w-full items-center gap-3 rounded-lg border px-4 py-3 text-left transition-colors
						{selected === agent.value
							? 'border-laya-orange bg-laya-orange/10'
							: $glassTheme ? 'border-white/[0.06] bg-white/[0.03] hover:border-white/[0.12]' : 'border-surface-600 bg-surface-900 hover:border-surface-500'}"
					onclick={() => selectAgent(agent.value)}
					disabled={saving}
				>
					<div class="flex h-4 w-4 items-center justify-center rounded-full border-2
						{selected === agent.value ? 'border-laya-orange' : 'border-surface-500'}">
						{#if selected === agent.value}
							<div class="h-2 w-2 rounded-full bg-laya-orange"></div>
						{/if}
					</div>
					<div class="flex-1">
						<div class="text-laya-base font-medium">{agent.label}</div>
						<div class="text-laya-secondary text-surface-400">{agent.description}</div>
					</div>
					{#if agent.value !== 'none' && agentPaths[agent.value]}
						<span class="text-laya-micro text-green-400/70" title={agentPaths[agent.value]}>detected</span>
					{:else if agent.value !== 'none'}
						<span class="text-laya-micro text-surface-500">not found</span>
					{/if}
				</button>
			{/each}
		</div>

	</div>

	<!-- Agent Binary Paths -->
	<div class="mt-4 {$glassTheme ? 'glass-section' : 'rounded-xl border border-surface-700 bg-surface-800'} p-4 {!hasAgent ? 'opacity-50' : ''}">
		<div class="mb-3 flex items-center justify-between">
			<div>
				<h3 class="text-laya-base font-medium">Agent Binary Path</h3>
				<p class="text-laya-secondary text-surface-400">
					{#if hasAgent}
						The agent's CLI binary. Detected automatically; choose another file to override it.
					{:else}
						Enable a coding agent above to configure the binary path
					{/if}
				</p>
			</div>
			{#if hasAgent}
				<button
					class="rounded-md border border-surface-600 bg-surface-700 px-3 py-1.5 text-laya-secondary font-medium transition-colors hover:bg-surface-600 disabled:opacity-50"
					onclick={detectPaths}
					disabled={detecting || !hasAgent}
				>
					{detecting ? 'Detecting...' : 'Auto-detect'}
				</button>
			{/if}
		</div>

		{#if hasAgent}
			{#key selected}
				<div class="flex flex-wrap items-center gap-2">
					{#if agentPaths[selected]}
						<code class="min-w-0 flex-1 truncate rounded-md bg-surface-900 px-3 py-2 font-mono text-laya-base text-surface-200" title={agentPaths[selected]}>{agentPaths[selected]}</code>
						<span class="text-laya-micro {customPath ? 'text-laya-amber' : 'text-green-400/70'}">{customPath ? 'chosen' : 'detected'}</span>
					{:else}
						<span class="flex-1 text-laya-secondary text-yellow-400/70">Not found. Install {AGENT_BINARY_NAMES[selected] || selected} or choose its binary.</span>
					{/if}
					<button
						class="rounded-md border border-surface-600 bg-surface-700 px-3 py-1.5 text-laya-secondary font-medium transition-colors hover:bg-surface-600"
						onclick={chooseBinary}
					>Choose file…</button>
					{#if customPath}
						<button class="text-laya-secondary text-surface-400 hover:text-surface-200" onclick={resetToDetected}>Reset to detected</button>
					{/if}
				</div>
				{#if customPath}
					<p class="mt-1.5 text-laya-micro text-surface-500">Detected: <span class="font-mono">{detectedPaths?.[selected] || 'nothing found'}</span></p>
				{/if}
				{#if typingPath}
					<form
						class="mt-2 flex items-center gap-2"
						onsubmit={(e) => {
							e.preventDefault();
							typingPath = false;
							if (typedPath.trim()) saveAgentPath(selected, typedPath.trim());
						}}
					>
						<!-- svelte-ignore a11y_autofocus -->
						<input
							type="text"
							autofocus
							aria-label="Binary path"
							bind:value={typedPath}
							spellcheck="false"
							class="flex-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2 font-mono text-laya-base text-surface-200 placeholder-surface-500 focus:border-laya-orange focus:outline-none"
							placeholder="/path/to/{AGENT_BINARY_NAMES[selected] || selected}"
						/>
						<button type="submit" class="rounded-md bg-surface-600 px-3 py-1.5 text-laya-secondary font-medium hover:bg-surface-500">Use</button>
						<button type="button" class="text-laya-secondary text-surface-400 hover:text-surface-200" onclick={() => (typingPath = false)}>Cancel</button>
					</form>
					<p class="mt-1 text-laya-micro text-surface-500">The file dialog is available in the desktop app; here the path is typed.</p>
				{/if}
			{/key}
		{/if}
	</div>

{/if}

<AssistantSettings />
