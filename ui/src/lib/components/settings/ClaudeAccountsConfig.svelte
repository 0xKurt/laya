<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	Settings → Claude accounts: the user's Claude subscriptions Laya spreads
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
		accountTitle,
		connectorAction,
		isLoginActive,
		loginStateText,
		planLabel,
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
	let loadError = $state<string | null>(null);
	let login = $state<ClaudeAccountLogin | null>(null);
	let code = $state('');
	let busy = $state<string | null>(null);
	let actionError = $state<string | null>(null);
	let confirmRemove = $state<string | null>(null);
	let copied = $state(false);
	let now = $state(Date.now());

	const sectionClass = $derived(
		$glassTheme ? 'glass-section' : 'rounded-xl border border-surface-700 bg-surface-800'
	);
	const loginActive = $derived(isLoginActive(login));

	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	async function load(refresh = false) {
		try {
			overview = await engineApi.getClaudeAccounts(refresh);
			loadError = null;
			if (overview.login && (isLoginActive(overview.login) || !login)) login = overview.login;
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Could not load the accounts';
		} finally {
			loading = false;
		}
	}

	function startPolling() {
		stopPolling();
		pollTimer = setInterval(async () => {
			if (!login) return stopPolling();
			try {
				const r = await engineApi.getClaudeAccountLogin(login.account_id);
				if (r.login) login = r.login;
			} catch {
				// The next tick tries again.
			}
			if (!isLoginActive(login)) {
				stopPolling();
				code = '';
				await load();
			}
		}, 1500);
	}

	function stopPolling() {
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = null;
	}

	onMount(() => {
		load().then(() => {
			if (isLoginActive(login)) startPolling();
		});
		clockTimer = setInterval(() => (now = Date.now()), 60_000);
	});

	onDestroy(() => {
		stopPolling();
		if (clockTimer) clearInterval(clockTimer);
	});

	async function run<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
		busy = key;
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

	async function addAccount() {
		const r = await run('add', () => engineApi.addClaudeAccount());
		if (r) {
			login = r;
			code = '';
			startPolling();
		}
	}

	async function signInAgain(id: string) {
		const r = await run(`signin:${id}`, () => engineApi.signInClaudeAccount(id));
		if (r) {
			login = r;
			code = '';
			startPolling();
		}
	}

	async function submitCode() {
		if (!login || !code.trim()) return;
		const r = await run('code', () => engineApi.submitClaudeAccountCode(login!.account_id, code));
		code = '';
		if (r) login = r;
	}

	async function cancelLogin() {
		if (!login) return;
		const r = await run('cancel', () => engineApi.cancelClaudeAccountLogin(login!.account_id));
		if (r) login = r;
		await load();
	}

	async function remove(id: string) {
		const r = await run(`remove:${id}`, () => engineApi.removeClaudeAccount(id));
		confirmRemove = null;
		if (r) {
			if (login?.account_id === id) login = null;
			await load();
		}
	}

	async function recheck(id: string) {
		const r = await run(`check:${id}`, () => engineApi.checkClaudeAccountConnectors(id));
		if (r) await load();
	}

	async function connectorClick(account: ClaudeAccount, key: ClaudeConnectorKey) {
		const c = account.connectors?.connectors[key];
		if (!c || !overview) return;
		const action = connectorAction(c, account.default);
		if (action === 'connect') {
			await openExternal(overview.connectors_url);
		} else if (action === 'add') {
			const r = await run(`conn:${account.id}:${key}`, () => engineApi.addClaudeAccountConnector(account.id, key));
			if (r) await load();
		} else if (action === 'login') {
			const r = await run(`conn:${account.id}:${key}`, () =>
				engineApi.signInClaudeAccountConnector(account.id, key)
			);
			if (r) {
				login = r;
				code = '';
				startPolling();
			}
		}
	}

	async function copyUrl() {
		if (!login?.url) return;
		try {
			await navigator.clipboard.writeText(login.url);
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
	const smallButton =
		'rounded-md border border-surface-600 px-2.5 py-1 text-laya-secondary text-surface-200 transition-colors hover:border-laya-orange hover:text-laya-orange disabled:opacity-50';
	const primaryButton =
		'rounded-md bg-laya-orange px-3 py-1.5 text-laya-secondary font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50';
</script>

{#snippet loginPanel(l: ClaudeAccountLogin)}
	<div class="mt-4 space-y-3 rounded-lg border border-surface-700 bg-surface-900 p-4">
		<div class="text-laya-base text-surface-200">{loginStateText(l)}</div>
		{#if l.state === 'waiting_code' && l.url}
			<div class="flex flex-wrap gap-2">
				<button class={primaryButton} onclick={() => l.url && openExternal(l.url)}>Open login page</button>
				<button class={smallButton} onclick={copyUrl}>{copied ? 'Copied' : 'Copy link'}</button>
			</div>
			{#if l.kind === 'account'}
				<p class="text-laya-secondary text-surface-400">
					The page signs in with the Claude account your browser is logged in to. To add another account, log in
					to it on claude.ai first or open the link in a private window.
				</p>
			{/if}
			<form
				class="flex gap-2"
				onsubmit={(e) => {
					e.preventDefault();
					submitCode();
				}}
			>
				<input
					type="password"
					autocomplete="off"
					spellcheck="false"
					class="min-w-0 flex-1 rounded-md border border-surface-600 bg-surface-800 px-3 py-1.5 font-mono text-laya-secondary text-surface-100"
					placeholder={l.kind === 'connector' ? 'Paste the address of the page you land on' : 'Paste code'}
					bind:value={code}
				/>
				<button type="submit" class={primaryButton} disabled={!code.trim() || busy === 'code'}>Continue</button>
			</form>
		{:else if l.state === 'starting' || l.state === 'verifying'}
			<div class="h-1 w-full overflow-hidden rounded bg-surface-700">
				<div class="h-full w-1/3 animate-pulse rounded bg-laya-orange"></div>
			</div>
		{/if}
		{#if isLoginActive(l)}
			<button class="text-laya-secondary text-surface-400 underline underline-offset-2 hover:text-surface-200" onclick={cancelLogin} disabled={busy === 'cancel'}>
				Cancel
			</button>
		{:else}
			<button class="text-laya-secondary text-surface-400 underline underline-offset-2 hover:text-surface-200" onclick={() => (login = null)}>
				Dismiss
			</button>
		{/if}
	</div>
{/snippet}

<div class="space-y-6">
	<div class="{sectionClass} p-6">
		<div class="flex items-start justify-between gap-4">
			<div>
				<h3 class="mb-1 text-laya-heading font-semibold text-surface-50">Claude accounts</h3>
				<p class="text-laya-base text-surface-400">
					Laya spreads its processing across accounts with headroom. Collecting, sending, My work and the terminal
					use your default account for now.
				</p>
			</div>
			<div class="flex shrink-0 gap-2">
				<button class={smallButton} onclick={() => load(true)} disabled={loading}>Refresh</button>
				<button class={primaryButton} onclick={addAccount} disabled={loginActive || busy === 'add'}>Add account</button>
			</div>
		</div>
		{#if login && login.kind === 'account' && login.state !== 'done'}
			{@render loginPanel(login)}
		{/if}
		{#if actionError}
			<div class="mt-4 rounded-lg border border-error-700/50 bg-error-900/20 p-3 text-laya-secondary text-error-300">{actionError}</div>
		{/if}
	</div>

	{#if loading}
		<div class="{sectionClass} p-6 text-laya-base text-surface-400">Loading accounts…</div>
	{:else if loadError}
		<div class="{sectionClass} p-6 text-laya-base text-error-400">{loadError}</div>
	{:else if overview}
		{#each overview.accounts as account (account.id)}
			<div class="{sectionClass} p-6">
				<div class="flex flex-wrap items-center gap-2">
					<span class="text-laya-base font-semibold text-surface-50">{accountTitle(account)}</span>
					{#if planLabel(account.plan)}
						<span class="rounded bg-surface-700 px-1.5 py-0.5 text-laya-micro text-surface-200">{planLabel(account.plan)}</span>
					{/if}
					{#if account.default}
						<span class="rounded bg-surface-700 px-1.5 py-0.5 text-laya-micro text-surface-300">Default</span>
					{/if}
					{#if overview.current === account.id}
						<span class="rounded bg-laya-orange/20 px-1.5 py-0.5 text-laya-micro font-medium text-laya-orange">Used for processing now</span>
					{/if}
					<span class="flex-1"></span>
					{#if !account.default}
						{#if !account.logged_in}
							<button class={smallButton} onclick={() => signInAgain(account.id)} disabled={loginActive}>Sign in again</button>
						{/if}
						{#if confirmRemove === account.id}
							<span class="text-laya-secondary text-surface-300">Remove this account from Laya?</span>
							<button class="rounded-md bg-error-600 px-2.5 py-1 text-laya-secondary text-white disabled:opacity-50" onclick={() => remove(account.id)} disabled={busy === `remove:${account.id}`}>
								{busy === `remove:${account.id}` ? 'Removing…' : 'Remove'}
							</button>
							<button class={smallButton} onclick={() => (confirmRemove = null)}>Keep</button>
						{:else}
							<button class={smallButton} onclick={() => (confirmRemove = account.id)}>Remove</button>
						{/if}
					{/if}
				</div>
				{#if account.problem}
					<p class="mt-2 text-laya-secondary text-amber-300">{account.problem}</p>
				{/if}

				<div class="mt-4 grid gap-3 sm:grid-cols-2">
					{#each usageBars(account, overview.thresholds, now) as bar (bar.key)}
						<div>
							<div class="mb-1 flex items-baseline justify-between text-laya-secondary">
								<span class="text-surface-300">{bar.label}</span>
								<span class="font-mono {bar.over ? 'text-amber-300' : 'text-surface-200'}">
									{bar.percent === null ? '—' : `${bar.percent} %`}
								</span>
							</div>
							<div class="relative h-2 overflow-hidden rounded bg-surface-700">
								<div
									class="h-full rounded {bar.over ? 'bg-amber-400' : 'bg-laya-orange'}"
									style="width: {bar.percent ?? 0}%"
								></div>
								<div class="absolute top-0 h-full w-px bg-surface-300/70" style="left: {bar.threshold}%" title="Laya moves on at {bar.threshold} %"></div>
							</div>
							{#if bar.resets}
								<div class="mt-1 text-laya-micro text-surface-500">{bar.resets}</div>
							{/if}
						</div>
					{/each}
				</div>
				<div class="mt-2 text-laya-micro text-surface-500">Usage {asOf(account.usage.as_of)}</div>

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
												{key === 'github' && c.state === 'connected' ? `gh${c.detail ? ` · ${c.detail}` : ''} · every account` : CONNECTOR_STATE_LABEL[c.state]}
											</div>
										</div>
										{#if action}
											<button class={smallButton} onclick={() => connectorClick(account, key)} disabled={busy === `conn:${account.id}:${key}` || (action === 'login' && loginActive)}>
												{ACTION_LABEL[action]}
											</button>
										{/if}
									</div>
								{/if}
							{/each}
						</div>
						{#if CONNECTOR_ORDER.some((k) => account.connectors?.connectors[k]?.kind === 'claude_ai' && account.connectors?.connectors[k]?.state !== 'connected')}
							<p class="mt-2 text-laya-micro text-surface-500">
								"Connect" opens claude.ai connector settings for whichever Claude account your browser is logged in to.
							</p>
						{/if}
					{:else}
						<p class="text-laya-secondary text-surface-500">Not checked yet. Re-check lists this account's connectors.</p>
					{/if}
					{#if login && login.kind === 'connector' && login.account_id === account.id && login.state !== 'done'}
						{@render loginPanel(login)}
					{/if}
				</div>

			</div>
		{/each}
	{/if}
</div>
