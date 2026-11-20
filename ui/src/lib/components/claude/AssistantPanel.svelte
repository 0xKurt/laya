<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { get } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { engineApi } from '$lib/api/engine';
	import type { ClaudeTerminalAgent, ClaudeTerminalSession } from '$lib/api/types';
	import TerminalView from '$lib/components/claude/TerminalView.svelte';
	import { takeQueued } from '$lib/claude/assistantReference';
	import { chatListOpen, chatOpen } from '$lib/stores/chat';
	import {
		activeTerminalId,
		assistantFocusRequest,
		assistantPendingReference,
		assistantReady,
		showClaudeToast
	} from '$lib/stores/claudeTerminal';
	import { PANEL_MAX, PANEL_MIN, sidePanel } from '$lib/stores/sidePanel';

	/**
	 * The docked assistant: one persistent terminal session running the coding
	 * agent, in a right-hand panel that survives navigation (mounted in the root
	 * layout, outside the route's {#key}). The process lives in the engine; the
	 * view attaches while the panel is open and replays the screen on reopen.
	 */

	type Status = 'idle' | 'starting' | 'running' | 'exited' | 'error';

	let session = $state<ClaudeTerminalSession | null>(null);
	let status = $state<Status>('idle');
	let notice = $state<string | null>(null);
	let error = $state<string | null>(null);
	let agents = $state<ClaudeTerminalAgent[]>([]);
	let agentChoice = $state<string>('');
	let freshStart = false;
	let loading = false;

	const shown = $derived($sidePanel.panel === 'assistant' && $sidePanel.host === 'panel');
	const agentLabel = $derived(
		session?.agent_label ?? agents.find((a) => a.id === agentChoice)?.label ?? 'Claude Code'
	);

	async function createSession(opts: { resume?: string } = {}): Promise<void> {
		status = 'starting';
		freshStart = true;
		const created = await engineApi.createClaudeTerminal(
			opts.resume ? { resume: opts.resume } : { name: 'Assistant', agent: agentChoice || undefined }
		);
		session = created;
		sidePanel.setSession(created.id);
		status = 'running';
	}

	/** Attach to the docked session, resume it after an engine restart, or start one. */
	async function ensureSession(): Promise<void> {
		if (loading) return;
		loading = true;
		error = null;
		try {
			const list = await engineApi.listClaudeTerminals();
			agents = list.agents ?? [];
			if (!agentChoice) agentChoice = list.default_agent ?? 'claude_code';
			const id = get(sidePanel).sessionId;
			const open = id ? list.sessions.find((s) => s.id === id) : undefined;
			if (open) {
				session = open;
				freshStart = false;
				status = open.running ? 'running' : 'exited';
				return;
			}
			const closed = id ? list.closed.find((c) => c.id === id) : undefined;
			if (closed?.resumable) {
				await createSession({ resume: closed.id });
				notice = 'Resumed the previous conversation.';
				return;
			}
			if (closed) {
				notice = 'The previous conversation could not be resumed (it has no saved transcript), so a new one was started.';
			}
			await createSession();
		} catch (e) {
			status = 'error';
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (shown && !session && status === 'idle') ensureSession();
		if (!shown) assistantReady.set(null);
	});

	// Re-check on every open: the session may have ended or the engine restarted.
	let wasShown = false;
	$effect(() => {
		if (shown && !wasShown && session) {
			session = null;
			status = 'idle';
		}
		wasShown = shown;
	});

	function onStarted() {
		const id = session?.id;
		if (!id) return;
		// A new process prints before its prompt reads input; give it a moment.
		setTimeout(() => {
			if (session?.id !== id || status !== 'running') return;
			assistantReady.set(id);
			flushReference(id);
		}, freshStart ? 1500 : 0);
	}

	async function flushReference(id: string) {
		const text = takeQueued(() => get(assistantPendingReference), () => assistantPendingReference.set(null));
		if (!text) return;
		try {
			await engineApi.sendClaudeTerminalInput(id, text);
			assistantFocusRequest.update((n) => n + 1);
			showClaudeToast('Sent to Assistant');
		} catch (e) {
			showClaudeToast(`Could not send to Assistant: ${e instanceof Error ? e.message : String(e)}`, 'warning');
		}
	}

	// A reference queued while the panel was already ready (e.g. right after reopening).
	$effect(() => {
		const pending = $assistantPendingReference;
		const ready = $assistantReady;
		if (pending && ready) flushReference(ready);
	});

	function onExit() {
		status = 'exited';
		assistantReady.set(null);
	}

	function onMissing() {
		// The engine lost the session (restart): resume it from its row.
		session = null;
		status = 'idle';
		assistantReady.set(null);
	}

	async function startAgain() {
		const old = session;
		notice = null;
		session = null;
		try {
			if (old) await engineApi.closeClaudeTerminal(old.id).catch(() => undefined);
			await createSession();
		} catch (e) {
			status = 'error';
			error = e instanceof Error ? e.message : String(e);
		}
	}

	function openInView() {
		const id = session?.id;
		if (!id) return;
		activeTerminalId.set(id);
		sidePanel.setHost('view');
		goto('/terminal');
	}

	function switchToChat() {
		chatListOpen.set(true);
		chatOpen.set(true);
	}

	// Drag the left edge to resize.
	let dragging = $state(false);
	function startResize(e: PointerEvent) {
		e.preventDefault();
		dragging = true;
		const target = e.currentTarget as HTMLElement;
		target.setPointerCapture(e.pointerId);
		const move = (ev: PointerEvent) => sidePanel.setWidth(window.innerWidth - ev.clientX - 16);
		const up = () => {
			dragging = false;
			target.removeEventListener('pointermove', move);
			target.removeEventListener('pointerup', up);
			target.removeEventListener('pointercancel', up);
		};
		target.addEventListener('pointermove', move);
		target.addEventListener('pointerup', up);
		target.addEventListener('pointercancel', up);
	}

	function resizeByKey(e: KeyboardEvent) {
		if (e.key === 'ArrowLeft') sidePanel.setWidth($sidePanel.width + 20);
		else if (e.key === 'ArrowRight') sidePanel.setWidth($sidePanel.width - 20);
	}
</script>

{#if shown}
	<aside
		class="fixed right-0 z-40 flex flex-col"
		style="top: var(--header-h, 45px); bottom: var(--footer-h, 33px); width: calc({$sidePanel.width}px + 16px);"
		aria-label="Assistant"
	>
		<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
		<div
			role="separator"
			aria-orientation="vertical"
			aria-label="Resize the assistant panel"
			aria-valuemin={PANEL_MIN}
			aria-valuemax={PANEL_MAX}
			aria-valuenow={$sidePanel.width}
			tabindex="0"
			class="absolute bottom-4 left-0 top-4 z-10 w-1.5 cursor-col-resize rounded transition-colors hover:bg-laya-orange/40 {dragging ? 'bg-laya-orange/60' : ''}"
			onpointerdown={startResize}
			onkeydown={resizeByKey}
		></div>
		<div class="my-4 mr-4 ml-1 flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-surface-700 bg-surface-900">
			<div class="flex h-11 shrink-0 items-center gap-2 border-b border-surface-700 px-3">
				<span class="h-1.5 w-1.5 shrink-0 rounded-full {status === 'running' ? 'bg-emerald-400' : 'bg-surface-600'}"></span>
				<h2 class="text-laya-secondary font-medium text-surface-100">Assistant</h2>
				<span class="truncate rounded bg-surface-800 px-1.5 py-0.5 text-laya-micro text-surface-400" title="Coding agent of this session">{agentLabel}</span>
				<div class="ml-auto flex shrink-0 items-center gap-1">
					<button
						type="button"
						class="rounded-md px-2 py-1 text-laya-micro font-medium text-surface-400 hover:bg-surface-800 hover:text-surface-100 disabled:opacity-40"
						disabled={!session}
						title="Show this session in the Assistant view"
						onclick={openInView}
					>Open in Assistant view</button>
					<button
						type="button"
						class="rounded-md px-2 py-1 text-laya-micro font-medium text-surface-400 hover:bg-surface-800 hover:text-surface-100"
						title="Switch the panel to Laya chat (L)"
						onclick={switchToChat}
					>Laya chat</button>
					<button
						type="button"
						aria-label="Close the assistant panel"
						title="Close (`)"
						class="flex h-6 w-6 items-center justify-center rounded text-surface-400 hover:bg-surface-800 hover:text-surface-100"
						onclick={() => sidePanel.close()}
					>
						<svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
					</button>
				</div>
			</div>

			{#if notice}
				<div class="mx-2 mt-2 flex items-start gap-2 rounded-lg bg-surface-800 px-3 py-1.5 text-laya-micro text-surface-300" role="status">
					<span class="flex-1">{notice}</span>
					<button type="button" class="text-surface-500 hover:text-surface-200" aria-label="Dismiss" onclick={() => (notice = null)}>×</button>
				</div>
			{/if}
			{#if session?.note}
				<div class="mx-2 mt-2 rounded-lg bg-amber-500/10 px-3 py-1.5 text-laya-micro text-amber-200/90">{session.note}</div>
			{/if}

			<div class="relative min-h-0 flex-1 bg-surface-950 p-1.5">
				{#if session && (status === 'running' || status === 'exited')}
					{#key session.id}
						<TerminalView
							sessionId={session.id}
							visible={true}
							fontSize={12}
							focusSignal={$assistantFocusRequest}
							onstarted={onStarted}
							onexit={onExit}
							onmissing={onMissing}
						/>
					{/key}
				{/if}
				{#if status === 'starting' || status === 'idle'}
					<div class="absolute inset-0 flex items-center justify-center text-laya-secondary text-surface-400">Starting {agentLabel}…</div>
				{/if}
				{#if status === 'exited' || status === 'error'}
					<div class="absolute inset-x-0 bottom-0 flex flex-wrap items-center justify-center gap-2 border-t border-surface-800 bg-surface-900/95 px-3 py-2">
						{#if error}<p class="w-full text-center text-laya-micro text-rose-300">{error}</p>{/if}
						<p class="text-laya-micro text-surface-400">{status === 'exited' ? 'The session has ended.' : ''}</p>
						{#if agents.length > 1}
							<select
								class="rounded-md border border-surface-700 bg-surface-950 px-1.5 py-1 text-laya-micro text-surface-200"
								aria-label="Agent"
								bind:value={agentChoice}
							>
								{#each agents as a (a.id)}
									<option value={a.id} disabled={!a.available}>{a.label}{a.available ? '' : ` — ${a.reason}`}</option>
								{/each}
							</select>
						{/if}
						<button
							type="button"
							class="rounded-md bg-laya-orange/20 px-3 py-1 text-laya-micro font-medium text-laya-orange hover:bg-laya-orange/30"
							onclick={startAgain}
						>Start again</button>
					</div>
				{/if}
			</div>
		</div>
	</aside>
{/if}
