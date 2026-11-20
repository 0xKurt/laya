<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Settings → logins: the user's coding agent subscriptions Laya spreads
	its processing over (engine/laya/claude_code/accounts.py). Lists each
	account with plan, usage windows and connectors; adds an account through
	the CLI's own sign-in (open the page, paste the code); removes one.
-->
<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import { glassTheme } from '$lib/stores/glassTheme';
	import { openExternal } from '$lib/utils/externalLinks';
	import { parseBackendDate, timeAgo } from '$lib/utils/datetime';
	import {
		CONNECTOR_ORDER,
		CONNECTOR_STATE_LABEL,
		accountBadge,
		accountTitle,
		connectorAction,
		countdown,
		isLoginActive,
		loginPlacement,
		loginStateText,
		openedLoginIds,
		planLabel,
		shouldAutoOpen,
		usageBars
	} from '$lib/utils/claudeAccounts';
	import type {
		ClaudeAccount,
		ClaudeAccountLogin,
		ClaudeAccountsOverview,
		ClaudeConnectorKey
	} from '$lib/api/types';

	let overview = $state<ClaudeAccountsOverview | null>(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let loadError = $state<string | null>(null);
	// The engine's sign-in state; the panel is rendered from it alone.
	let login = $state<ClaudeAccountLogin | null>(null);
	// What the user typed into a sign-in's paste field, by sign-in id.
	let drafts = $state<Record<string, string>>({});
	let dismissed = $state<Set<string>>(new Set());
	let busy = $state<string | null>(null);
	// Errors shown in the card they belong to ('top' = the header).
	let errors = $state<Record<string, string>>({});
	let confirmRemove = $state<string | null>(null);
	let copied = $state(false);
	let now = $state(Date.now());

	const sectionClass = $derived(
		$glassTheme ? 'glass-section' : 'rounded-xl border border-surface-700 bg-surface-800'
	);
	const loginActive = $derived(isLoginActive(login));
	const placement = $derived(loginPlacement(login, overview?.accounts ?? [], dismissed));
	const ownAccounts = $derived((overview?.accounts ?? []).filter((a) => !a.default));
	const cliLogin = $derived((overview?.accounts ?? []).find((a) => a.default) ?? null);

	let loginTimer: ReturnType<typeof setInterval> | null = null;
	let listTimer: ReturnType<typeof setInterval> | null = null;
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	async function load(refresh = false) {
		if (refresh) refreshing = true;
		try {
			overview = await engineApi.getClaudeAccounts(refresh);
			loadError = null;
			applyLogin(overview.login);
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Could not load the accounts';
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	function applyLogin(next: ClaudeAccountLogin | null) {
		const wasActive = isLoginActive(login);
		login = next;
		if (shouldAutoOpen(next, openedLoginIds) && next?.url) {
			openedLoginIds.add(next.id);
			void openExternal(next.url);
		}
		if (wasActive && !isLoginActive(next)) void load();
	}

	async function pollLogin() {
		try {
			applyLogin((await engineApi.getClaudeLogin()).login);
		} catch {
			// The next tick tries again.
		}
	}

	onMount(() => {
		load();
		loginTimer = setInterval(pollLogin, 2000);
		listTimer = setInterval(() => {
			if (!busy) void load();
		}, 60_000);
		clockTimer = setInterval(() => (now = Date.now()), 1000);
	});

	onDestroy(() => {
		for (const t of [loginTimer, listTimer, clockTimer]) if (t) clearInterval(t);
	});

	async function run<T>(key: string, target: string, fn: () => Promise<T>): Promise<T | undefined> {
		busy = key;
		errors = Object.fromEntries(Object.entries(errors).filter(([k]) => k !== target));
		try {
			return await fn();
		} catch (e) {
			errors = { ...errors, [target]: e instanceof Error ? e.message : 'Something went wrong' };
			return undefined;
		} finally {
			busy = null;
		}
	}

	async function startedLogin(r: ClaudeAccountLogin | undefined) {
		if (!r) return;
		applyLogin(r);
		await load();
	}

	async function addAccount() {
		await startedLogin(await run('add', 'top', () => engineApi.addClaudeAccount()));
	}

	async function signInAgain(id: string) {
		await startedLogin(await run(`signin:${id}`, id, () => engineApi.signInClaudeAccount(id)));
	}

	async function submitCode(l: ClaudeAccountLogin) {
		const code = (drafts[l.id] ?? '').trim();
		if (!code) return;
		const r = await run('code', placementKey(l), () => engineApi.submitClaudeAccountCode(l.account_id, code));
		if (r) {
			drafts = { ...drafts, [l.id]: '' };
			applyLogin(r);
		}
	}

	async function cancelLogin(l: ClaudeAccountLogin) {
		const r = await run('cancel', placementKey(l), () => engineApi.cancelClaudeAccountLogin(l.account_id));
		if (r) applyLogin(r);
		await load();
	}

	function dismiss(l: ClaudeAccountLogin) {
		dismissed = new Set([...dismissed, l.id]);
	}

	function placementKey(l: ClaudeAccountLogin): string {
		return placement === 'new' ? 'new' : l.account_id;
	}

	async function remove(id: string) {
		const r = await run(`remove:${id}`, id, () => engineApi.removeClaudeAccount(id));
		confirmRemove = null;
		if (r) await load();
	}

	async function recheck(id: string) {
		const r = await run(`check:${id}`, id, () => engineApi.checkClaudeAccountConnectors(id));
		if (r) await load();
	}

	async function connectorClick(account: ClaudeAccount, key: ClaudeConnectorKey) {
		const c = account.connectors?.connectors[key];
		if (!c || !overview) return;
		const action = connectorAction(c, account.default);
		if (action === 'connect') {
			await openExternal(overview.connectors_url);
		} else if (action === 'add') {
			const r = await run(`conn:${account.id}:${key}`, account.id, () =>
				engineApi.addClaudeAccountConnector(account.id, key)
			);
			if (r) await load();
		} else if (action === 'login') {
			await startedLogin(
				await run(`conn:${account.id}:${key}`, account.id, () =>
					engineApi.signInClaudeAccountConnector(account.id, key)
				)
			);
		}
	}

	async function copyUrl(url: string | null) {
		if (!url) return;
		try {
			await navigator.clipboard.writeText(url);
			copied = true;
			setTimeout(() => (copied = false), 1500);
		} catch {
			// Clipboard access can be refused; the button to open the page remains.
		}
	}

	function asOf(ts: string | null): string {
		if (!ts) return 'no usage reported yet';
		const d = parseBackendDate(ts);
		const time = d ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) : '';
		return `as of ${time} (${timeAgo(ts)})`;
	}

	const ACTION_LABEL: Record<string, string> = { connect: 'Connect', add: 'Add', login: 'Log in' };
	const STATE_TONE: Record<string, string> = {
		connected: 'text-green-400',
		needs_login: 'text-amber-300',
		failed: 'text-error-400',
		pending: 'text-surface-400',
		disabled: 'text-surface-500',
		missing: 'text-surface-500'
	};
	const BADGE_TONE: Record<string, string> = {
		active: 'bg-laya-orange/20 text-laya-orange',
		muted: 'bg-surface-700 text-surface-300',
		warn: 'bg-amber-500/20 text-amber-300'
	};
	const smallButton =
		'rounded-md border border-surface-600 px-2.5 py-1 text-laya-secondary text-surface-200 transition-colors hover:border-laya-orange hover:text-laya-orange disabled:opacity-50';
	const primaryButton =
		'rounded-md bg-laya-orange px-3 py-1.5 text-laya-secondary font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50';
</script>

{#snippet loginPanel(l: ClaudeAccountLogin)}
	{@const left = countdown(l.expires_at, now)}
	<div class="mt-4 space-y-3 rounded-lg border border-laya-orange/40 bg-surface-900 p-4">
		<div class="flex items-baseline justify-between gap-3">
			<div class="text-laya-base text-surface-200">{loginStateText(l)}</div>
			{#if isLoginActive(l) && left}
				<span class="shrink-0 font-mono text-laya-micro text-surface-400">{left}</span>
			{/if}
		</div>
		{#if l.state === 'waiting_code' && l.url}
			<div class="flex flex-wrap gap-2">
				<button class={primaryButton} onclick={() => l.url && openExternal(l.url)}>
					{l.kind === 'connector' ? 'Open the page again' : 'Open login page'}
				</button>
				<button class={smallButton} onclick={() => copyUrl(l.url)}>{copied ? 'Copied' : 'Copy link'}</button>
			</div>
			{#if l.kind === 'account'}
				<p class="text-laya-secondary text-surface-400">
					The page signs in with the account your browser is logged in to. To add another account, log in
					to it on claude.ai first or open the link in a private window.
				</p>
			{:else}
				<p class="text-laya-secondary text-surface-400">
					The page opened in your browser and finishes by itself. If the browser shows an error page after you
					approve, paste that page's address here.
				</p>
			{/if}
			<form
				class="flex gap-2"
				onsubmit={(e) => {
					e.preventDefault();
					submitCode(l);
				}}
			>
				<input
					type="password"
					autocomplete="off"
					spellcheck="false"
					class="min-w-0 flex-1 rounded-md border border-surface-600 bg-surface-800 px-3 py-1.5 font-mono text-laya-secondary text-surface-100"
					placeholder={l.kind === 'connector' ? 'Paste redirect URL' : 'Paste code'}
					value={drafts[l.id] ?? ''}
					oninput={(e) => (drafts = { ...drafts, [l.id]: (e.currentTarget as HTMLInputElement).value })}
				/>
				<button type="submit" class={primaryButton} disabled={!(drafts[l.id] ?? '').trim() || busy === 'code'}>
					Continue
				</button>
			</form>
		{:else if l.state === 'starting' || l.state === 'verifying'}
			<div class="h-1 w-full overflow-hidden rounded bg-surface-700">
				<div class="h-full w-1/3 animate-pulse rounded bg-laya-orange"></div>
			</div>
		{/if}
		{#if isLoginActive(l)}
			<button
				class="text-laya-secondary text-surface-400 underline underline-offset-2 hover:text-surface-200"
				onclick={() => cancelLogin(l)}
				disabled={busy === 'cancel'}
			>
				Cancel
			</button>
		{:else}
			<button
				class="text-laya-secondary text-surface-400 underline underline-offset-2 hover:text-surface-200"
				onclick={() => dismiss(l)}
			>
				Dismiss
			</button>
		{/if}
	</div>
{/snippet}

{#snippet cardError(key: string)}
	{#if errors[key]}
		<div class="mt-3 rounded-lg border border-error-700/50 bg-error-900/20 p-3 text-laya-secondary text-error-300">
			{errors[key]}
		</div>
	{/if}
{/snippet}

{#snippet accountCard(account: ClaudeAccount)}
	{@const badge = accountBadge(account, overview?.current ?? null)}
	<div class="{sectionClass} p-6 {account.default && !account.in_use ? 'opacity-80' : ''}">
		{#if account.default}
			<div class="mb-1 text-laya-micro uppercase tracking-wider text-surface-400">
				Claude Code · CLI login (used only while no login is added)
			</div>
		{/if}
		<div class="flex flex-wrap items-center gap-2">
			<span class="text-laya-base font-semibold text-surface-50">{accountTitle(account)}</span>
			{#if planLabel(account.plan)}
				<span class="rounded bg-surface-700 px-1.5 py-0.5 text-laya-micro text-surface-200">{planLabel(account.plan)}</span>
			{/if}
			{#if badge}
				<span class="rounded px-1.5 py-0.5 text-laya-micro font-medium {BADGE_TONE[badge.tone]}">{badge.label}</span>
			{/if}
			<span class="flex-1"></span>
			{#if !account.default}
				{#if !account.logged_in && !account.pending}
					<button class={smallButton} onclick={() => signInAgain(account.id)} disabled={loginActive}>Sign in again</button>
				{/if}
				{#if confirmRemove === account.id}
					<span class="text-laya-secondary text-surface-300">Remove this account from Laya?</span>
					<button
						class="rounded-md bg-error-600 px-2.5 py-1 text-laya-secondary text-white disabled:opacity-50"
						onclick={() => remove(account.id)}
						disabled={busy === `remove:${account.id}`}
					>
						{busy === `remove:${account.id}` ? 'Removing…' : 'Remove'}
					</button>
					<button class={smallButton} onclick={() => (confirmRemove = null)}>Keep</button>
				{:else if !account.pending}
					<button class={smallButton} onclick={() => (confirmRemove = account.id)}>Remove</button>
				{/if}
			{/if}
		</div>
		{#if account.problem}
			<p class="mt-2 text-laya-secondary text-amber-300">{account.problem}</p>
		{/if}

		{#if placement === account.id && login}
			{@render loginPanel(login)}
		{/if}
		{@render cardError(account.id)}

		{#if !account.pending && account.logged_in}
			<div class="mt-4 grid gap-3 sm:grid-cols-2">
				{#each usageBars(account, overview?.thresholds ?? {}, now) as bar (bar.key)}
					<div>
						<div class="mb-1 flex items-baseline justify-between text-laya-secondary">
							<span class="text-surface-300">{bar.label}</span>
							<span class="font-mono {bar.over ? 'text-amber-300' : 'text-surface-200'}">
								{bar.rejected ? 'limit reached' : bar.percent === null ? '—' : `${bar.percent} %`}
							</span>
						</div>
						<div class="relative h-2 overflow-hidden rounded bg-surface-700">
							<div
								class="h-full rounded {bar.over ? 'bg-amber-400' : 'bg-laya-orange'}"
								style="width: {bar.rejected ? 100 : (bar.percent ?? 0)}%"
							></div>
							<div
								class="absolute top-0 h-full w-px bg-surface-300/70"
								style="left: {bar.threshold}%"
								title="Laya moves on at {bar.threshold} %"
							></div>
						</div>
						{#if bar.resets}
							<div class="mt-1 text-laya-micro text-surface-500">{bar.resets}</div>
						{/if}
					</div>
				{/each}
			</div>
			{#if account.usage.models?.length}
				<div class="mt-2 flex flex-wrap gap-x-4 text-laya-micro text-surface-500">
					{#each account.usage.models as m (m.label)}
						<span>Week ({m.label}): {m.percent} %{m.resets ? ` · resets ${m.resets}` : ''}</span>
					{/each}
				</div>
			{/if}
			<div class="mt-2 text-laya-micro text-surface-500">Usage {asOf(account.usage.as_of)}</div>
		{/if}

		{#if !account.pending}
			<div class="mt-5">
				<div class="mb-2 flex items-center justify-between">
					<span class="text-laya-micro uppercase tracking-wider text-surface-400">Connectors</span>
					<span class="flex items-center gap-3">
						{#if account.connectors}
							<span class="text-laya-micro text-surface-500">checked {timeAgo(account.connectors.checked_at)}</span>
						{/if}
						<button class={smallButton} onclick={() => recheck(account.id)} disabled={busy === `check:${account.id}`}>
							{busy === `check:${account.id}` ? 'Checking…' : 'Re-check'}
						</button>
					</span>
				</div>
				{#if account.connectors?.error}
					<p class="mb-2 text-laya-secondary text-error-400">{account.connectors.error}</p>
				{/if}
				{#if account.connectors}
					<div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
						{#each CONNECTOR_ORDER as key (key)}
							{@const c = account.connectors.connectors[key]}
							{#if c}
								{@const action = connectorAction(c, account.default)}
								<div class="flex items-center justify-between gap-2 rounded-lg border border-surface-700 bg-surface-900 px-3 py-2">
									<div class="min-w-0">
										<div class="text-laya-secondary font-medium text-surface-100">{c.label}</div>
										<div class="truncate text-laya-micro {STATE_TONE[c.state] ?? 'text-surface-400'}">
											{key === 'github' && c.state === 'connected'
												? `gh${c.detail ? ` · ${c.detail}` : ''} · every account`
												: CONNECTOR_STATE_LABEL[c.state]}
										</div>
									</div>
									{#if action}
										<button
											class={smallButton}
											onclick={() => connectorClick(account, key)}
											disabled={busy === `conn:${account.id}:${key}` || (action === 'login' && loginActive)}
											title={action === 'login' && loginActive ? 'Another sign-in is running' : undefined}
										>
											{ACTION_LABEL[action]}
										</button>
									{/if}
								</div>
							{/if}
						{/each}
					</div>
					{#if CONNECTOR_ORDER.some((k) => account.connectors?.connectors[k]?.kind === 'claude_ai' && account.connectors?.connectors[k]?.state !== 'connected')}
						<p class="mt-2 text-laya-micro text-surface-500">
							"Connect" opens claude.ai connector settings for whichever account your browser is logged in to.
						</p>
					{/if}
				{:else}
					<p class="text-laya-secondary text-surface-500">Not checked yet. Re-check lists this account's connectors.</p>
				{/if}
			</div>
		{/if}
	</div>
{/snippet}

<div class="space-y-6">
	<div class="{sectionClass} p-6">
		<div class="flex items-start justify-between gap-4">
			<div>
				<h3 class="mb-1 text-laya-heading font-semibold text-surface-50">Logins</h3>
				<p class="text-laya-base text-surface-400">
					Laya runs on the accounts you add here, independently of the account the Claude Code CLI is logged in to.
					It moves to the account with most headroom when one nears its limits. Collecting, My work and actions on a
					platform run only on an account where that platform's connector is connected.
				</p>
			</div>
			<div class="flex shrink-0 gap-2">
				<button class={smallButton} onclick={() => load(true)} disabled={loading || refreshing}>
					{refreshing ? 'Refreshing…' : 'Refresh'}
				</button>
				<button class={primaryButton} onclick={addAccount} disabled={loginActive || busy === 'add'}>Add account</button>
			</div>
		</div>
		{@render cardError('top')}
	</div>

	{#if loading}
		<div class="{sectionClass} p-6 text-laya-base text-surface-400">Loading accounts…</div>
	{:else if loadError}
		<div class="{sectionClass} p-6 text-laya-base text-error-400">{loadError}</div>
	{:else if overview}
		{#if placement === 'new' && login}
			<div class="{sectionClass} p-6">
				<span class="text-laya-base font-semibold text-surface-50">New account</span>
				{@render loginPanel(login)}
				{@render cardError('new')}
			</div>
		{/if}
		{#each ownAccounts as account (account.id)}
			{@render accountCard(account)}
		{/each}
		{#if cliLogin}
			{@render accountCard(cliLogin)}
		{/if}
	{/if}
</div>
