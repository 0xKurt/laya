<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Setup wizard, step 3: connect tools through the user's coding agent login
	instead of n8n (docs/claude-code-connector.md). Runs discovery, shows
	each platform with what was found or what is missing, and connects them.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import PlatformIcon from '$lib/components/settings/PlatformIcon.svelte';
	import {
		CLAUDE_CODE_LABELS,
		CLAUDE_CODE_PLATFORMS,
		claudeCodeProblem,
		identityLabel
	} from '$lib/utils/claudeCode';
	import type { ClaudeCodeIdentity, ClaudeCodePlatform, ClaudeCodeStatus } from '$lib/api/types';

	let {
		identities = $bindable({}),
		onUseN8n
	}: {
		/** The identities discovery found, per platform; the wizard prefills
		 *  the "me" team member from them. */
		identities?: Partial<Record<ClaudeCodePlatform, ClaudeCodeIdentity>>;
		onUseN8n: () => void;
	} = $props();

	let status = $state<ClaudeCodeStatus | null>(null);
	let discovering = $state(false);
	let error = $state<string | null>(null);
	let connecting = $state<Set<ClaudeCodePlatform>>(new Set());
	let connectErrors = $state<Partial<Record<ClaudeCodePlatform, string>>>({});

	const cliFound = $derived(status?.cli.found ?? false);
	const ready = $derived(
		CLAUDE_CODE_PLATFORMS.filter((p) => status?.platforms[p]?.available && !status.platforms[p].connected)
	);
	const connectedCount = $derived(CLAUDE_CODE_PLATFORMS.filter((p) => status?.platforms[p]?.connected).length);

	function collectIdentities(s: ClaudeCodeStatus) {
		const out: Partial<Record<ClaudeCodePlatform, ClaudeCodeIdentity>> = {};
		for (const p of CLAUDE_CODE_PLATFORMS) {
			const id = s.platforms[p]?.identity;
			if (id && Object.keys(id).length > 0) out[p] = id;
		}
		identities = out;
	}

	function setStatus(s: ClaudeCodeStatus) {
		status = s;
		collectIdentities(s);
	}

	async function discover() {
		discovering = true;
		error = null;
		try {
			setStatus(await engineApi.discoverClaudeCode());
		} catch (e) {
			error = e instanceof Error ? e.message : 'Discovery failed';
		} finally {
			discovering = false;
		}
	}

	async function connect(p: ClaudeCodePlatform) {
		connecting = new Set([...connecting, p]);
		connectErrors = { ...connectErrors, [p]: undefined };
		try {
			const next = await engineApi.connectClaudeCode(p);
			if (status) setStatus({ ...status, platforms: { ...status.platforms, [p]: next } });
		} catch (e) {
			connectErrors = { ...connectErrors, [p]: e instanceof Error ? e.message : 'Could not connect' };
		} finally {
			const rest = new Set(connecting);
			rest.delete(p);
			connecting = rest;
		}
	}

	async function connectAll() {
		// One at a time: each connect writes team.json and rules.json.
		for (const p of ready) await connect(p);
	}

	onMount(() => {
		// Discovery reads identities too, so the team step can be prefilled.
		discover();
	});

	function segments(text: string): { code: boolean; text: string }[] {
		return text.split('`').map((t, i) => ({ code: i % 2 === 1, text: t }));
	}
</script>

<div class="space-y-4">
	<h2 class="text-xl font-semibold">Connect tools through your coding agent</h2>
	<p class="text-sm text-surface-400">
		Laya reads Slack, Gmail, Linear, GitHub and Notion through the connectors your coding agent login already
		has (Claude Code connectors, or MCP servers added to Codex). No Slack app, OAuth client or n8n is needed. Collecting is read-only; Laya sends a reply only when you
		click Send on a card.
	</p>

	<div class="rounded-md border border-surface-600 bg-surface-700 p-4">
		<div class="flex items-center gap-3">
			<span
				class="h-3 w-3 rounded-full
					{discovering && !status
					? 'bg-surface-500 animate-pulse'
					: cliFound
						? 'bg-green-500'
						: 'bg-red-500'}"
			></span>
			<span class="flex-1 text-sm font-medium">
				{#if discovering && !status}
					Looking for coding agents and their connectors…
				{:else if cliFound}
					Claude Code {status?.cli.version ?? ''} found
				{:else}
					Claude Code was not found
				{/if}
			</span>
			<button
				onclick={discover}
				disabled={discovering}
				class="rounded-md bg-surface-600 px-3 py-1.5 text-xs font-medium transition-colors hover:bg-surface-500 disabled:opacity-50"
			>
				{discovering ? 'Checking…' : 'Check again'}
			</button>
		</div>
		{#if discovering}
			<p class="mt-2 text-xs text-surface-400">This asks your coding agent which tools it has and who you are there. It can take a minute.</p>
		{/if}
		{#if status && !cliFound}
			<p class="mt-2 text-sm text-surface-400">
				Install a coding agent (Claude Code or Codex) and log in to it once in a terminal, then check again.
			</p>
		{/if}
		{#if error}
			<p class="mt-2 text-sm text-red-400">{error}</p>
		{/if}
	</div>

	{#if status && cliFound}
		<div class="space-y-2">
			{#each CLAUDE_CODE_PLATFORMS as p (p)}
				{@const s = status.platforms[p]}
				{@const problem = claudeCodeProblem(s, cliFound)}
				{@const who = identityLabel(s?.identity)}
				<div
					class="flex items-start gap-3 rounded-md border p-3
						{s?.connected
						? 'border-green-500/30 bg-green-500/5'
						: s?.available
							? 'border-surface-600 bg-surface-800'
							: 'border-surface-700 bg-surface-800/50'}"
				>
					<span class="mt-0.5 shrink-0 {s?.available ? 'text-surface-100' : 'text-surface-500'}">
						<PlatformIcon platform={p} size={18} />
					</span>
					<div class="min-w-0 flex-1">
						<div class="flex items-center gap-2">
							<span class="text-sm font-medium">{CLAUDE_CODE_LABELS[p]}</span>
							<span
								class="rounded px-1.5 py-0.5 text-[11px] font-medium
									{s?.connected
									? 'bg-green-500/15 text-green-400'
									: s?.available
										? 'bg-laya-orange/15 text-laya-orange'
										: 'bg-surface-700 text-surface-400'}"
							>
								{s?.connected ? 'Connected' : s?.available ? 'Found' : 'Missing'}
							</span>
						</div>
						{#if s?.available && who}
							<p class="mt-0.5 truncate text-xs text-surface-400">as {who}</p>
						{/if}
						{#if problem}
							<p class="mt-0.5 text-xs text-yellow-400/90">
								{#each segments(problem) as seg, i (i)}
									{#if seg.code}<code class="rounded bg-surface-700 px-1">{seg.text}</code>{:else}{seg.text}{/if}
								{/each}
							</p>
						{/if}
						{#if connectErrors[p]}
							<p class="mt-0.5 text-xs text-red-400">{connectErrors[p]}</p>
						{/if}
					</div>
					{#if s?.available && !s.connected}
						<button
							onclick={() => connect(p)}
							disabled={connecting.has(p) || discovering}
							class="shrink-0 rounded-md bg-laya-orange px-3 py-1.5 text-xs font-semibold text-integ-on-accent transition-colors hover:bg-laya-gold disabled:opacity-50"
						>
							{connecting.has(p) ? 'Connecting…' : 'Connect'}
						</button>
					{:else if s?.connected}
						<span class="shrink-0 text-xs text-green-400">✓</span>
					{/if}
				</div>
			{/each}
		</div>

		<div class="flex flex-wrap items-center gap-3">
			{#if ready.length > 1}
				<button
					onclick={connectAll}
					disabled={connecting.size > 0 || discovering}
					class="rounded-md bg-laya-orange px-4 py-2 text-sm font-semibold text-integ-on-accent transition-colors hover:bg-laya-gold disabled:opacity-50"
				>
					{connecting.size > 0 ? 'Connecting…' : `Connect all available (${ready.length})`}
				</button>
			{/if}
			{#if connectedCount > 0}
				<span class="text-sm text-green-400">
					{connectedCount} connected. Settings → Integrations has the options per tool.
				</span>
			{/if}
		</div>
	{/if}

	<p class="text-xs text-surface-500">
		You can connect more later in Settings → Integrations.
		<button onclick={onUseN8n} class="text-surface-400 underline underline-offset-2 hover:text-surface-200">
			Use n8n instead
		</button>
	</p>
</div>
