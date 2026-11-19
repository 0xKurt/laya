<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	One platform collected through the user's Claude Code login
	(docs/claude-code-connector.md): connect / disconnect, status, sync now,
	pause, and the platform's settings.
-->
<script lang="ts">
	import { untrack } from 'svelte';
	import { slide } from 'svelte/transition';
	import { engineApi } from '$lib/api/engine';
	import { reducedMotion } from '$lib/stores/reducedMotion';
	import { timeAgo } from '$lib/utils/datetime';
	import { STATUS_STYLE, statusTint } from '$lib/utils/integrationHealth';
	import {
		CLAUDE_CODE_LABELS,
		MODELS,
		WEEKDAYS,
		claudeCodeProblem,
		claudeCodeState,
		configToForm,
		eventsLabel,
		formToConfig,
		identityLabel,
		type ClaudeCodeForm
	} from '$lib/utils/claudeCode';
	import type { ClaudeCodePlatform, ClaudeCodePlatformStatus, ClaudeCodeStatus } from '$lib/api/types';

	let {
		platform,
		status,
		cliFound,
		onChange
	}: {
		platform: ClaudeCodePlatform;
		/** This platform's entry of GET /claude-code/status; null while loading. */
		status: ClaudeCodePlatformStatus | null;
		cliFound: boolean;
		/** Called after any change: with the full status when the call returned
		 *  one (discovery), else without, and the parent reloads. */
		onChange: (full?: ClaudeCodeStatus) => void;
	} = $props();

	const label = $derived(CLAUDE_CODE_LABELS[platform]);
	const ccState = $derived(claudeCodeState(status));
	const connected = $derived(ccState !== 'off');
	const problem = $derived(claudeCodeProblem(status, cliFound));

	const BADGE: Record<string, { text: string; status: 'connected' | 'error' | 'expired' }> = {
		ok: { text: 'Connected', status: 'connected' },
		paused: { text: 'Paused', status: 'expired' },
		error: { text: 'Error', status: 'error' },
		unavailable: { text: 'Unavailable', status: 'error' }
	};

	const INTRO: Record<ClaudeCodePlatform, string> = {
		slack: 'Reads your DMs, mentions and the threads you are in through the Slack connector of your Claude Code login. No Slack app or n8n needed.',
		gmail: 'Reads your inbox through the Gmail connector of your Claude Code login, including meeting notes. No OAuth client or n8n needed.',
		linear: 'Reads the issues you are involved in through the Linear connector of your Claude Code login. No API key or n8n needed.',
		github: 'Reads your pull requests, reviews and mentions through the GitHub CLI (gh). No token or n8n needed.',
		notion: 'Reads pages and comments that involve you through the Notion connector of your Claude Code login. No integration or n8n needed.'
	};

	let busy = $state<null | 'connect' | 'disconnect' | 'sync' | 'discover' | 'enable' | 'save'>(null);
	let actionError = $state<string | null>(null);
	let syncResult = $state<string | null>(null);
	let confirming = $state(false);
	let showSettings = $state(false);
	let saved = $state(false);

	// The form follows the stored config; editing it does not, so a reload
	// after save (or a change elsewhere) resets it, typing does not.
	let form = $state<ClaudeCodeForm | null>(null);
	let formError = $state<string | null>(null);
	const storedKey = $derived(`${platform}:${JSON.stringify(status?.config ?? null)}`);
	$effect(() => {
		storedKey;
		untrack(() => {
			form = status ? configToForm(status.config) : null;
			formError = null;
		});
	});

	const timezones: string[] = (() => {
		try {
			return (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone') ?? [];
		} catch {
			return [];
		}
	})();

	async function run<T>(kind: NonNullable<typeof busy>, fn: () => Promise<T>): Promise<T | undefined> {
		busy = kind;
		actionError = null;
		try {
			return await fn();
		} catch (e) {
			actionError = e instanceof Error ? e.message : 'Something went wrong';
			return undefined;
		} finally {
			busy = null;
		}
	}

	async function connect() {
		const r = await run('connect', () => engineApi.connectClaudeCode(platform));
		if (r) {
			syncResult = null;
			onChange();
		}
	}

	async function disconnect() {
		const r = await run('disconnect', () => engineApi.disconnectClaudeCode(platform));
		if (r) {
			confirming = false;
			syncResult = null;
			onChange();
		}
	}

	async function sync() {
		syncResult = null;
		const r = await run('sync', () => engineApi.syncClaudeCode(platform));
		if (r) {
			syncResult = r.error ? null : `Synced — ${eventsLabel(r.events)}.`;
			if (r.error) actionError = r.error;
			onChange();
		}
	}

	async function discover() {
		const r = await run('discover', () => engineApi.discoverClaudeCode());
		if (r) onChange(r);
	}

	async function toggleEnabled() {
		if (!status) return;
		const r = await run('enable', () => engineApi.setClaudeCodeEnabled(platform, !status.enabled));
		if (r) onChange();
	}

	async function save() {
		if (!form) return;
		const result = formToConfig(platform, form);
		if (result.error !== null) {
			formError = result.error;
			return;
		}
		formError = null;
		const config = result.config;
		const r = await run('save', () => engineApi.updateClaudeCodeConfig(platform, config));
		if (r) {
			saved = true;
			setTimeout(() => (saved = false), 2500);
			onChange();
		}
	}

	function toggleDay(d: number) {
		if (!form) return;
		form.days = form.days.includes(d) ? form.days.filter((x) => x !== d) : [...form.days, d].sort((a, b) => a - b);
	}

	/** Problem text with `code` spans, e.g. "Run `gh auth login`." */
	function segments(text: string): { code: boolean; text: string }[] {
		return text.split('`').map((t, i) => ({ code: i % 2 === 1, text: t }));
	}

	const inputClass =
		'w-full rounded-lg border border-integ-input-border bg-integ-input-bg px-3 py-[7px] text-integ-body text-integ-t1 outline-none placeholder:text-integ-t4 focus:border-laya-orange focus:ring-[3px] focus:ring-integ-accent-tint';
	const labelClass = 'mb-1 block text-integ-note font-medium text-integ-t3';
	const helpClass = 'mt-1 text-integ-small leading-normal text-integ-t4';
	const linkButton = 'text-integ-body text-integ-t3 transition-colors hover:text-integ-t1 disabled:opacity-50';
	const primaryButton =
		'rounded-[7px] bg-laya-orange px-3 py-[5px] text-integ-body font-semibold text-integ-on-accent transition-colors hover:bg-laya-gold disabled:opacity-45 disabled:hover:bg-laya-orange';

	const badge = $derived(BADGE[ccState]);
	const failing = $derived(ccState === 'error' || ccState === 'unavailable');
</script>

<svelte:window
	onkeydown={(e) => {
		if (confirming && e.key === 'Escape') confirming = false;
	}}
/>

<div
	class="flex flex-col gap-3.5 rounded-[11px] bg-integ-card px-[18px] py-4 {failing ? '' : 'border-[0.5px] border-integ-line'}"
	style:border={failing ? `1px solid ${statusTint('error', 0.35)}` : undefined}
>
	<!-- Top row -->
	<div class="flex flex-wrap items-center gap-2.5 gap-y-2.5">
		<span
			class="h-2 w-2 shrink-0 rounded-full"
			style:background={badge ? STATUS_STYLE[badge.status].dot : 'var(--color-integ-t4)'}
		></span>
		<span class="text-integ-name font-semibold text-integ-t1">via Claude Code</span>
		{#if badge}
			<span
				class="rounded-[5px] px-[7px] py-0.5 text-integ-label font-semibold tracking-[0.02em]"
				style:background={statusTint(badge.status, 0.14)}
				style:color={STATUS_STYLE[badge.status].fg}
			>
				{badge.text}
			</span>
		{/if}
		<span class="flex-1"></span>
		{#if !confirming}
			<div class="flex items-center gap-3.5">
				{#if connected}
					<button onclick={sync} disabled={busy !== null} class={linkButton}>
						{busy === 'sync' ? 'Syncing…' : 'Sync now'}
					</button>
					<button
						onclick={() => (confirming = true)}
						disabled={busy !== null}
						class="text-integ-body text-integ-err-fg opacity-70 transition-opacity hover:opacity-100"
					>
						Disconnect
					</button>
				{:else}
					<button onclick={connect} disabled={busy !== null || !!problem || !status} class={primaryButton}>
						{busy === 'connect' ? 'Connecting…' : 'Connect'}
					</button>
				{/if}
			</div>
		{/if}
	</div>

	{#if !connected}
		<p class="max-w-[520px] text-integ-body leading-[1.6] text-integ-t3 [text-wrap:pretty]">{INTRO[platform]}</p>
		{#if busy === 'connect'}
			<p class="text-integ-small text-integ-t4">Reading your {label} identity through Claude Code. This can take a minute.</p>
		{/if}
	{:else if status && identityLabel(status.identity)}
		<span class="text-integ-body text-integ-t3">Signed in as <span class="text-integ-t2">{identityLabel(status.identity)}</span></span>
	{/if}

	<!-- What is missing, and what to do about it -->
	{#if problem}
		<div
			class="flex flex-col gap-2 rounded-[7px] px-3 py-[9px] text-integ-note leading-[1.55]"
			style:background={statusTint('expired', 0.08)}
			style:color={STATUS_STYLE.expired.fg}
		>
			<span class="[overflow-wrap:anywhere]">
				{#each segments(problem) as s, i (i)}
					{#if s.code}<code class="rounded bg-integ-fill-2 px-1 font-mono">{s.text}</code>{:else}{s.text}{/if}
				{/each}
			</span>
			<span class="text-integ-small text-integ-t3">
				When it is fixed,
				<button onclick={discover} disabled={busy !== null} class="font-medium text-integ-t1 underline underline-offset-2 disabled:opacity-50">
					{busy === 'discover' ? 'checking again…' : 'run discovery again'}
				</button>.
			</span>
		</div>
	{/if}

	{#if actionError}
		<div
			class="select-text rounded-[7px] px-3 py-[9px] font-mono text-integ-note leading-[1.55] [overflow-wrap:anywhere]"
			style:background={statusTint('error', 0.08)}
			style:color={STATUS_STYLE.error.fg}
			role="alert"
		>
			{actionError}
		</div>
	{:else if connected && status?.last_error}
		<div
			class="select-text rounded-[7px] px-3 py-[9px] font-mono text-integ-note leading-[1.55] [overflow-wrap:anywhere]"
			style:background={statusTint('error', 0.08)}
			style:color={STATUS_STYLE.error.fg}
		>
			Last sweep failed: {status.last_error}
		</div>
	{/if}

	{#if syncResult}
		<div
			class="rounded-[7px] px-3 py-2 text-integ-body"
			style:background={statusTint('connected', 0.1)}
			style:color={STATUS_STYLE.connected.fg}
			role="status"
		>
			{syncResult}
		</div>
	{/if}

	{#if connected && status}
		<div class="grid grid-cols-3 gap-3">
			<div class="flex flex-col gap-[3px]">
				<span class="text-integ-label uppercase tracking-[0.06em] text-integ-t4">Last sync</span>
				<span class="font-mono text-integ-body text-integ-t2">{timeAgo(status.last_run_at, { nullLabel: 'not yet' })}</span>
			</div>
			<div class="flex flex-col gap-[3px]">
				<span class="text-integ-label uppercase tracking-[0.06em] text-integ-t4">Last OK</span>
				<span class="font-mono text-integ-body text-integ-t2">{timeAgo(status.last_ok_at, { nullLabel: '—' })}</span>
			</div>
			<div class="flex flex-col gap-[3px]">
				<span class="text-integ-label uppercase tracking-[0.06em] text-integ-t4">Last sweep</span>
				<span class="font-mono text-integ-body text-integ-t2">
					{status.last_run_at ? eventsLabel(status.last_count) : '—'}
				</span>
			</div>
		</div>

		<!-- Collect automatically -->
		<div class="flex items-center gap-3 border-t-[0.5px] border-integ-line pt-3">
			<button
				role="switch"
				aria-checked={status.enabled}
				aria-label="Collect automatically"
				onclick={toggleEnabled}
				disabled={busy !== null}
				class="relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-60 {status.enabled ? 'bg-green-600' : 'bg-integ-fill-2'}"
			>
				<span class="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform {status.enabled ? 'left-[1.125rem]' : 'left-0.5'}"></span>
			</button>
			<div class="flex min-w-0 flex-1 flex-col">
				<span class="text-integ-body font-medium text-integ-t2">Collect automatically</span>
				<span class="text-integ-small text-integ-t4">
					{status.enabled
						? `Every ${status.config.interval_minutes} min during your active hours.`
						: 'Paused. Sync now still works.'}
				</span>
			</div>
			{#if status.can_send}
				<span class="rounded-[5px] bg-integ-fill px-2 py-[3px] font-mono text-integ-chip text-integ-t2"
					title="Replies are sent only when you click Send on a card">can reply</span>
			{/if}
		</div>

		<!-- Settings -->
		<div class="flex flex-col gap-3 border-t-[0.5px] border-integ-line pt-3">
			<button
				onclick={() => (showSettings = !showSettings)}
				aria-expanded={showSettings}
				class="flex items-center gap-2 self-start text-integ-body font-medium text-integ-t3 transition-colors hover:text-integ-t1"
			>
				<svg class="h-3.5 w-3.5 transition-transform {showSettings ? 'rotate-90' : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M9 5l7 7-7 7" stroke-linecap="round" stroke-linejoin="round" />
				</svg>
				Settings
			</button>

			{#if showSettings && form}
				<div transition:slide={{ duration: $reducedMotion ? 0 : 180 }} class="flex flex-col gap-4">
					<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
						<div>
							<label for="cc-{platform}-interval" class={labelClass}>Check every (minutes)</label>
							<input id="cc-{platform}-interval" type="number" min="1" bind:value={form.interval_minutes} class={inputClass} />
						</div>
						<div>
							<label for="cc-{platform}-model" class={labelClass}>Model</label>
							<select id="cc-{platform}-model" bind:value={form.model} class={inputClass}>
								{#each MODELS as m (m.value)}
									<option value={m.value}>{m.label}</option>
								{/each}
							</select>
						</div>
					</div>

					<div>
						<span class={labelClass}>Active hours</span>
						<div class="flex flex-wrap items-center gap-2">
							<input type="time" aria-label="From" bind:value={form.start} class="{inputClass} !w-auto" />
							<span class="text-integ-body text-integ-t4">to</span>
							<input type="time" aria-label="Until" bind:value={form.end} class="{inputClass} !w-auto" />
						</div>
						<div class="mt-2 flex flex-wrap gap-1.5" role="group" aria-label="Days">
							{#each WEEKDAYS as day, d (day)}
								<button
									onclick={() => toggleDay(d)}
									aria-pressed={form.days.includes(d)}
									class="rounded-[6px] px-2.5 py-1 text-integ-small font-medium transition-colors
										{form.days.includes(d) ? 'bg-integ-accent-tint text-integ-t1' : 'bg-integ-fill text-integ-t4 hover:text-integ-t2'}"
								>
									{day}
								</button>
							{/each}
						</div>
					</div>

					<div>
						<label for="cc-{platform}-tz" class={labelClass}>Time zone</label>
						<input
							id="cc-{platform}-tz"
							list="cc-timezones"
							bind:value={form.timezone}
							placeholder="This computer's time zone"
							class={inputClass}
						/>
						<datalist id="cc-timezones">
							{#each timezones as tz (tz)}
								<option value={tz}></option>
							{/each}
						</datalist>
					</div>

					<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
						<div>
							<label for="cc-{platform}-lookback" class={labelClass}>First look-back (hours)</label>
							<input id="cc-{platform}-lookback" type="number" min="0" bind:value={form.initial_lookback_hours} class={inputClass} />
							<p class={helpClass}>How far back the first sync reads.</p>
						</div>
						<div>
							<label for="cc-{platform}-recheck" class={labelClass}>Re-check open cards after (hours)</label>
							<input id="cc-{platform}-recheck" type="number" min="0" bind:value={form.recheck_after_hours} class={inputClass} />
							<p class={helpClass}>Closes cards that were answered or resolved in {label}.</p>
						</div>
					</div>

					{#if platform === 'slack'}
						<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
							<div>
								<label for="cc-slack-context" class={labelClass}>Earlier messages shown</label>
								<input id="cc-slack-context" type="number" min="0" bind:value={form.context_messages} class={inputClass} />
								<p class={helpClass}>Context included with each new message.</p>
							</div>
							<div>
								<label for="cc-slack-memory" class={labelClass}>Remember threads for (days)</label>
								<input id="cc-slack-memory" type="number" min="0" bind:value={form.thread_memory_days} class={inputClass} />
								<p class={helpClass}>Threads you wrote in keep being followed this long.</p>
							</div>
						</div>
						<label class="flex items-start gap-2 text-integ-body text-integ-t2">
							<input type="checkbox" bind:checked={form.only_relevant} class="mt-0.5 h-4 w-4 rounded accent-laya-orange" />
							<span>
								Only what involves me
								<span class="block text-integ-small text-integ-t4">DMs, mentions, your own messages and threads you wrote in.</span>
							</span>
						</label>
					{:else if platform === 'gmail'}
						<div>
							<label for="cc-gmail-exclude" class={labelClass}>Skip mail from</label>
							<textarea id="cc-gmail-exclude" rows="4" bind:value={form.exclude_senders} class="{inputClass} font-mono"></textarea>
							<p class={helpClass}>One address per line. Notifications Laya already gets from the tools themselves.</p>
						</div>
						<label class="flex items-start gap-2 text-integ-body text-integ-t2">
							<input type="checkbox" bind:checked={form.meeting_notes} class="mt-0.5 h-4 w-4 rounded accent-laya-orange" />
							<span>
								Meeting notes
								<span class="block text-integ-small text-integ-t4">Turn action items from Gemini meeting notes into cards.</span>
							</span>
						</label>
					{:else if platform === 'github'}
						<div>
							<label for="cc-github-orgs" class={labelClass}>Organizations</label>
							<input id="cc-github-orgs" bind:value={form.orgs} placeholder="All organizations" class={inputClass} />
							<p class={helpClass}>Comma separated. Empty means every organization you belong to.</p>
						</div>
						<label class="flex items-start gap-2 text-integ-body text-integ-t2">
							<input type="checkbox" bind:checked={form.others_prs_only_on_mention} class="mt-0.5 h-4 w-4 rounded accent-laya-orange" />
							<span>
								Others' pull requests only when I am mentioned
								<span class="block text-integ-small text-integ-t4">Your own pull requests are always followed.</span>
							</span>
						</label>
					{/if}

					{#if formError}
						<p class="text-integ-note text-integ-err-fg" role="alert">{formError}</p>
					{/if}
					<div class="flex items-center gap-3">
						<button onclick={save} disabled={busy !== null} class={primaryButton}>
							{busy === 'save' ? 'Saving…' : 'Save settings'}
						</button>
						{#if saved}
							<span class="text-integ-small" style:color={STATUS_STYLE.connected.fg}>Saved</span>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}

	{#if !problem}
		<div class="flex items-center gap-2 text-integ-small text-integ-t4">
			<span>Added or removed a connector in Claude Code?</span>
			<button onclick={discover} disabled={busy !== null} class="font-medium text-integ-t3 underline-offset-2 hover:text-integ-t1 hover:underline disabled:opacity-50">
				{busy === 'discover' ? 'Checking…' : 'Run discovery again'}
			</button>
		</div>
	{/if}

	{#if confirming}
		<div class="flex flex-wrap items-center gap-3 rounded-lg bg-[rgb(239_68_68/0.08)] px-3.5 py-3">
			<div class="flex min-w-[220px] flex-1 flex-col gap-0.5">
				<span class="text-integ-item font-semibold text-integ-t1">Disconnect {label} via Claude Code?</span>
				<span class="text-integ-note leading-normal text-integ-t3">
					Stops collecting and removes its source and seeded rules. Existing cards stay. Your Claude Code connector is not touched.
				</span>
			</div>
			<!-- svelte-ignore a11y_autofocus -->
			<button autofocus onclick={() => (confirming = false)} class="rounded-[7px] bg-integ-fill-2 px-3 py-1.5 text-integ-body text-integ-t2">
				Cancel
			</button>
			<button
				onclick={disconnect}
				disabled={busy !== null}
				class="rounded-[7px] bg-[#dc2626] px-3 py-1.5 text-integ-body font-semibold text-white transition-colors hover:bg-[#ef4444] disabled:opacity-60"
			>
				{busy === 'disconnect' ? 'Disconnecting…' : 'Disconnect'}
			</button>
		</div>
	{/if}
</div>
