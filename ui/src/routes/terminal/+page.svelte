<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { engineApi } from '$lib/api/engine';
	import type { ClaudeTerminalClosedSession, ClaudeTerminalSession } from '$lib/api/types';
	import TerminalView from '$lib/components/claude/TerminalView.svelte';
	import { activeTerminalId, pendingReference, showClaudeToast } from '$lib/stores/claudeTerminal';
	import { timeAgo } from '$lib/utils/datetime';

	// Sessions run in the engine; this page lists them and attaches a view to each.
	let sessions = $state<ClaudeTerminalSession[]>([]);
	let closed = $state<ClaudeTerminalClosedSession[]>([]);
	let defaultCwd = $state('');
	let activeId = $state<string | null>(get(activeTerminalId));
	let loading = $state(true);
	let error = $state<string | null>(null);
	let starting = $state(false);

	let newOpen = $state(false);
	let newCwd = $state('');
	let newName = $state('');
	let resumeOpen = $state(false);
	let renamingId = $state<string | null>(null);
	let renameValue = $state('');
	let confirmCloseId = $state<string | null>(null);

	// A reference to insert once the tab with this id has started.
	let referenceFor: { id: string; text: string } | null = null;

	const active = $derived(sessions.find((s) => s.id === activeId) ?? null);
	const recentCwds = $derived(
		[...new Set([...sessions.map((s) => s.cwd), ...closed.map((c) => c.cwd), defaultCwd].filter(Boolean))].slice(0, 10)
	);

	function activate(id: string | null) {
		activeId = id;
		activeTerminalId.set(id);
	}

	async function refresh() {
		const list = await engineApi.listClaudeTerminals();
		sessions = list.sessions;
		closed = list.closed;
		defaultCwd = list.default_cwd;
		if (!sessions.some((s) => s.id === activeId)) activate(sessions.at(-1)?.id ?? null);
	}

	async function start(opts: { cwd?: string; name?: string; resume?: string } = {}): Promise<ClaudeTerminalSession | null> {
		if (starting) return null;
		starting = true;
		error = null;
		try {
			const created = await engineApi.createClaudeTerminal({
				cwd: opts.cwd?.trim() || undefined,
				name: opts.name?.trim() || undefined,
				resume: opts.resume
			});
			sessions = [...sessions.filter((s) => s.id !== created.id), created];
			closed = closed.filter((c) => c.id !== created.id);
			activate(created.id);
			return created;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			return null;
		} finally {
			starting = false;
		}
	}

	async function startFromForm() {
		const created = await start({ cwd: newCwd, name: newName });
		if (created) {
			newOpen = false;
			newName = '';
		}
	}

	async function resume(id: string) {
		resumeOpen = false;
		await start({ resume: id });
	}

	async function closeSession(id: string) {
		confirmCloseId = null;
		try {
			await engineApi.closeClaudeTerminal(id);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
		const idx = sessions.findIndex((s) => s.id === id);
		sessions = sessions.filter((s) => s.id !== id);
		if (activeId === id) activate(sessions[Math.min(idx, sessions.length - 1)]?.id ?? null);
		try {
			closed = (await engineApi.listClaudeTerminals()).closed;
		} catch {
			// The list refreshes on the next visit.
		}
	}

	function requestClose(s: ClaudeTerminalSession) {
		if (s.running) confirmCloseId = s.id;
		else closeSession(s.id);
	}

	function beginRename(s: ClaudeTerminalSession) {
		renamingId = s.id;
		renameValue = s.name;
	}

	async function commitRename() {
		const id = renamingId;
		renamingId = null;
		if (!id) return;
		const name = renameValue.trim();
		const current = sessions.find((s) => s.id === id);
		if (!name || !current || name === current.name) return;
		try {
			const updated = await engineApi.renameClaudeTerminal(id, name);
			sessions = sessions.map((s) => (s.id === id ? updated : s));
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	function markExited(id: string, code: number | null) {
		sessions = sessions.map((s) => (s.id === id ? { ...s, running: false, exit_code: code } : s));
	}

	function onStarted(id: string) {
		if (!referenceFor || referenceFor.id !== id) return;
		const { text } = referenceFor;
		referenceFor = null;
		// claude's first output arrives before its prompt reads input; give it a moment.
		setTimeout(async () => {
			try {
				await engineApi.sendClaudeTerminalInput(id, text);
				showClaudeToast('Referenced in Claude');
			} catch (e) {
				showClaudeToast(`Could not reference in Claude: ${e instanceof Error ? e.message : String(e)}`, 'warning');
			}
		}, 1500);
	}

	async function takePendingReference() {
		const text = get(pendingReference);
		if (!text) return;
		pendingReference.set(null);
		const running = sessions.filter((s) => s.running);
		const target = running.find((s) => s.id === activeId) ?? running.at(-1);
		if (target) {
			activate(target.id);
			try {
				await engineApi.sendClaudeTerminalInput(target.id, text);
				showClaudeToast('Referenced in Claude');
			} catch (e) {
				showClaudeToast(`Could not reference in Claude: ${e instanceof Error ? e.message : String(e)}`, 'warning');
			}
			return;
		}
		const created = await start();
		if (created) referenceFor = { id: created.id, text };
	}

	onMount(() => {
		(async () => {
			try {
				await refresh();
				newCwd = defaultCwd;
				await takePendingReference();
			} catch (e) {
				error = e instanceof Error ? e.message : String(e);
			} finally {
				loading = false;
			}
		})();
		// A reference made while this page is open (e.g. from a card detail panel).
		const unsubscribe = pendingReference.subscribe((text) => {
			if (text && !loading) takePendingReference();
		});
		return unsubscribe;
	});

	function closedLabel(c: ClaudeTerminalClosedSession): string {
		const when = c.closed_at ?? c.last_active_at ?? c.created_at;
		return `${c.name} · ${c.cwd.replace(/^\/Users\/[^/]+/, '~')} · ${timeAgo(when)}`;
	}
</script>

<svelte:head><title>Claude · Laya</title></svelte:head>

<div class="flex h-full min-h-0 flex-col">
	<!-- Tab bar -->
	<div class="flex shrink-0 items-center gap-1 border-b border-surface-800 px-3 py-1.5">
		<div class="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto" role="tablist" aria-label="Claude sessions">
			{#each sessions as s (s.id)}
				<div
					class="group relative flex max-w-[16rem] shrink-0 items-center rounded-md text-laya-secondary transition-colors
						{s.id === activeId ? 'bg-surface-800 text-surface-100' : 'text-surface-400 hover:bg-surface-800/60 hover:text-surface-200'}"
				>
					{#if renamingId === s.id}
						<!-- svelte-ignore a11y_autofocus -->
						<input
							class="w-40 rounded bg-surface-900 px-2 py-1 text-laya-secondary text-surface-100 outline-none ring-1 ring-laya-orange/50"
							bind:value={renameValue}
							autofocus
							onblur={commitRename}
							onkeydown={(e) => {
								if (e.key === 'Enter') commitRename();
								if (e.key === 'Escape') renamingId = null;
							}}
						/>
					{:else}
						<button
							type="button"
							role="tab"
							aria-selected={s.id === activeId}
							class="flex min-w-0 items-center gap-1.5 py-1 pl-2.5 pr-1"
							title={`${s.cwd}\nDouble-click to rename`}
							onclick={() => activate(s.id)}
							ondblclick={() => beginRename(s)}
						>
							<span class="h-1.5 w-1.5 shrink-0 rounded-full {s.running ? 'bg-emerald-400' : 'bg-surface-600'}"></span>
							<span class="truncate">{s.name}</span>
						</button>
					{/if}
					<button
						type="button"
						aria-label="Close {s.name}"
						title="Close"
						class="mr-1 flex h-5 w-5 shrink-0 items-center justify-center rounded text-surface-500 hover:bg-surface-700 hover:text-surface-200"
						onclick={() => requestClose(s)}
					>
						<svg class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
					</button>
					{#if confirmCloseId === s.id}
						<div class="absolute left-0 top-full z-30 mt-1 w-60 rounded-lg border border-surface-700 bg-surface-900 p-3 shadow-xl">
							<p class="text-laya-secondary text-surface-200">Stop claude and close this tab?</p>
							<p class="mt-1 text-laya-micro text-surface-500">You can resume the conversation later from “Resume…”.</p>
							<div class="mt-2 flex justify-end gap-2">
								<button type="button" class="rounded px-2 py-1 text-laya-micro text-surface-400 hover:text-surface-200" onclick={() => (confirmCloseId = null)}>Cancel</button>
								<button type="button" class="rounded bg-rose-500/20 px-2 py-1 text-laya-micro font-medium text-rose-200 hover:bg-rose-500/30" onclick={() => closeSession(s.id)}>Close</button>
							</div>
						</div>
					{/if}
				</div>
			{/each}
			<button
				type="button"
				aria-label="New Claude session"
				title="New session in {defaultCwd || 'the default folder'}"
				disabled={starting}
				class="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-surface-400 hover:bg-surface-800 hover:text-surface-100 disabled:opacity-40"
				onclick={() => start()}
			>
				<svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 5v14M5 12h14" /></svg>
			</button>
		</div>

		<div class="relative flex shrink-0 items-center gap-1">
			<button
				type="button"
				class="rounded-md px-2 py-1 text-laya-micro font-medium text-surface-400 hover:bg-surface-800 hover:text-surface-100"
				onclick={() => {
					newOpen = !newOpen;
					resumeOpen = false;
					if (!newCwd) newCwd = defaultCwd;
				}}
			>New in…</button>
			<button
				type="button"
				class="rounded-md px-2 py-1 text-laya-micro font-medium text-surface-400 hover:bg-surface-800 hover:text-surface-100"
				onclick={() => {
					resumeOpen = !resumeOpen;
					newOpen = false;
				}}
			>Resume…</button>

			{#if newOpen}
				<form
					class="absolute right-0 top-full z-30 mt-1 w-96 rounded-lg border border-surface-700 bg-surface-900 p-3 shadow-xl"
					onsubmit={(e) => {
						e.preventDefault();
						startFromForm();
					}}
				>
					<label class="block text-laya-micro font-medium text-surface-400" for="terminal-cwd">Folder</label>
					<input
						id="terminal-cwd"
						list="terminal-cwds"
						class="mt-1 w-full rounded-md border border-surface-700 bg-surface-950 px-2 py-1 font-mono text-laya-micro text-surface-100 outline-none focus:border-laya-orange/60"
						bind:value={newCwd}
						placeholder={defaultCwd}
					/>
					<datalist id="terminal-cwds">
						{#each recentCwds as cwd (cwd)}<option value={cwd}></option>{/each}
					</datalist>
					<label class="mt-2 block text-laya-micro font-medium text-surface-400" for="terminal-name">Name (optional)</label>
					<input
						id="terminal-name"
						class="mt-1 w-full rounded-md border border-surface-700 bg-surface-950 px-2 py-1 text-laya-micro text-surface-100 outline-none focus:border-laya-orange/60"
						bind:value={newName}
						placeholder="Folder name"
					/>
					<div class="mt-3 flex justify-end gap-2">
						<button type="button" class="rounded px-2 py-1 text-laya-micro text-surface-400 hover:text-surface-200" onclick={() => (newOpen = false)}>Cancel</button>
						<button type="submit" disabled={starting} class="rounded bg-laya-orange/20 px-2.5 py-1 text-laya-micro font-medium text-laya-orange hover:bg-laya-orange/30 disabled:opacity-40">Start</button>
					</div>
				</form>
			{/if}

			{#if resumeOpen}
				<div class="absolute right-0 top-full z-30 mt-1 max-h-96 w-[28rem] overflow-y-auto rounded-lg border border-surface-700 bg-surface-900 py-1 shadow-xl">
					{#if closed.length === 0}
						<p class="px-3 py-2 text-laya-micro text-surface-500">No closed sessions.</p>
					{/if}
					{#each closed as c (c.id)}
						<button
							type="button"
							disabled={!c.resumable || starting}
							title={c.resumable ? `Resume in ${c.cwd}` : 'Claude has no conversation for this session (it was closed before the first message).'}
							class="block w-full truncate px-3 py-1.5 text-left text-laya-micro text-surface-300 hover:bg-surface-800 hover:text-surface-100 disabled:cursor-not-allowed disabled:opacity-40"
							onclick={() => resume(c.id)}
						>{closedLabel(c)}</button>
					{/each}
				</div>
			{/if}
		</div>
	</div>

	{#if error}
		<div class="mx-3 mt-2 flex items-start gap-2 rounded-lg bg-rose-500/15 px-3 py-2 text-laya-secondary text-rose-300">
			<span class="flex-1">{error}</span>
			<button type="button" class="text-rose-300/70 hover:text-rose-200" onclick={() => (error = null)} aria-label="Dismiss">×</button>
		</div>
	{/if}
	{#if active?.note}
		<div class="mx-3 mt-2 rounded-lg bg-amber-500/10 px-3 py-1.5 text-laya-micro text-amber-200/90">{active.note}</div>
	{/if}

	<!-- Terminals: one view per open session; only the active one is shown. -->
	<div class="relative min-h-0 flex-1 bg-surface-950 p-2">
		{#each sessions as s (s.id)}
			<TerminalView
				sessionId={s.id}
				visible={s.id === activeId}
				onstarted={() => onStarted(s.id)}
				onexit={(code) => markExited(s.id, code)}
				oninput={() => activeTerminalId.set(s.id)}
			/>
		{/each}
		{#if !loading && sessions.length === 0}
			<div class="flex h-full flex-col items-center justify-center gap-3 text-center">
				<p class="text-laya-secondary text-surface-400">No Claude session is open.</p>
				<button
					type="button"
					disabled={starting}
					class="rounded-md bg-laya-orange/20 px-3 py-1.5 text-laya-secondary font-medium text-laya-orange hover:bg-laya-orange/30 disabled:opacity-40"
					onclick={() => start()}
				>{starting ? 'Starting…' : 'Start Claude'}</button>
				<p class="text-laya-micro text-surface-500">in {defaultCwd}</p>
			</div>
		{/if}
	</div>
</div>
