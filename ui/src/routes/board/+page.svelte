<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { onMount } from 'svelte';
	import CardDetail from '$lib/components/feed/CardDetail.svelte';
	import ActionCardComponent from '$lib/components/feed/ActionCard.svelte';
	import { engineApi } from '$lib/api/engine';
	import type { ActionCard, BoardLane } from '$lib/api/types';
	import { lastMessage } from '$lib/stores/websocket';
	import { timeAgo } from '$lib/utils/datetime';
	import { PRIORITY_COLORS, PRIORITY_LABELS, platformDotColor, platformKey, platformLabel } from '$lib/utils/cardVisuals';

	type Column = BoardLane | 'done';

	const COLUMNS: { id: Column; title: string }[] = [
		{ id: 'backlog', title: 'Backlog' },
		{ id: 'todo', title: 'To do' },
		{ id: 'in_progress', title: 'In progress' },
		{ id: 'done', title: 'Done' }
	];
	// A card running an agent is in progress, whatever lane it was dropped in.
	const AGENT_STATUSES = new Set(['agent_running', 'awaiting_input', 'executing']);
	const PRIORITY_RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

	let cards = $state<ActionCard[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let selected = $state<ActionCard | null>(null);
	let dragging = $state<ActionCard | null>(null);
	let dropTarget = $state<Column | 'archive' | null>(null);

	type BoardView = 'detailed' | 'compact';
	const VIEW_KEY = 'laya.board.view';
	function storedView(): BoardView {
		try {
			return localStorage.getItem(VIEW_KEY) === 'compact' ? 'compact' : 'detailed';
		} catch {
			return 'detailed';
		}
	}
	let view = $state<BoardView>(storedView());
	function setView(next: BoardView) {
		view = next;
		try {
			localStorage.setItem(VIEW_KEY, next);
		} catch {
			// private storage unavailable: the choice lasts for this session only
		}
	}

	function columnOf(card: ActionCard): Column {
		if (card.status === 'done') return 'done';
		if (AGENT_STATUSES.has(card.status)) return 'in_progress';
		return card.board_lane ?? 'backlog';
	}

	function activity(card: ActionCard): string {
		return card.event_timestamp ?? card.created_at ?? '';
	}

	const columns = $derived.by(() => {
		const byColumn: Record<Column, ActionCard[]> = { backlog: [], todo: [], in_progress: [], done: [] };
		for (const card of cards) byColumn[columnOf(card)].push(card);
		for (const id of ['backlog', 'todo', 'in_progress'] as const) {
			byColumn[id].sort(
				(a, b) =>
					(PRIORITY_RANK[a.priority] ?? 9) - (PRIORITY_RANK[b.priority] ?? 9) ||
					activity(b).localeCompare(activity(a))
			);
		}
		return byColumn;
	});

	async function load() {
		try {
			cards = await engineApi.getBoard();
			error = null;
			if (selected) {
				const fresh = cards.find((c) => c.card_id === selected!.card_id);
				if (!fresh) selected = null;
			}
		} catch (e: any) {
			error = e?.message ?? 'Could not load the board';
		} finally {
			loading = false;
		}
	}

	async function openCard(card: ActionCard) {
		try {
			// The board list is slim; the detail panel needs drafts and actions.
			selected = await engineApi.getCard(card.card_id);
		} catch {
			selected = card;
		}
	}

	async function moveTo(card: ActionCard, target: Column | 'archive') {
		const from = columnOf(card);
		if (target === from) return;
		const previous = cards;
		// Optimistic: move first, reload from the engine afterwards.
		if (target === 'archive') {
			cards = cards.filter((c) => c.card_id !== card.card_id);
		} else {
			cards = cards.map((c) =>
				c.card_id === card.card_id
					? { ...c, status: target === 'done' ? 'done' : c.status === 'done' ? 'ready' : c.status, board_lane: target === 'done' ? c.board_lane : target }
					: c
			);
		}
		try {
			if (target === 'archive') {
				await engineApi.archiveCard(card.card_id);
				if (selected?.card_id === card.card_id) selected = null;
			} else if (target === 'done') {
				await engineApi.markCardDone(card.card_id);
			} else {
				if (card.status === 'done') await engineApi.reopenCard(card.card_id);
				await engineApi.setCardLane(card.card_id, target);
			}
		} catch (e: any) {
			cards = previous;
			error = e?.body?.detail ?? e?.message ?? 'Could not move the card';
		}
		await load();
	}

	let archivingAll = $state(false);
	async function archiveAllDone() {
		const done = columns.done;
		archivingAll = true;
		cards = cards.filter((c) => c.status !== 'done');
		if (selected?.status === 'done') selected = null;
		const results = await Promise.allSettled(done.map((c) => engineApi.archiveCard(c.card_id)));
		const failed = results.filter((r) => r.status === 'rejected').length;
		if (failed) error = `${failed} of ${done.length} cards could not be archived`;
		archivingAll = false;
		await load();
	}

	async function runAgent(entityId: string) {
		const card = selected;
		try {
			await engineApi.runEntityAgent(entityId);
			if (card && card.board_lane !== 'in_progress') await engineApi.setCardLane(card.card_id, 'in_progress');
		} catch (e: any) {
			error = e?.body?.detail ?? e?.message ?? 'Failed to start agent';
		}
		await load();
	}

	function onDragStart(e: DragEvent, card: ActionCard) {
		dragging = card;
		e.dataTransfer?.setData('text/plain', card.card_id);
		if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
	}

	function onDrop(e: DragEvent, target: Column | 'archive') {
		e.preventDefault();
		const card = dragging;
		dragging = null;
		dropTarget = null;
		if (card) moveTo(card, target);
	}

	// Reload on any card change pushed by the engine (new cards, status changes).
	let reloadTimer: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const msg = $lastMessage;
		if (!msg || !['card_created', 'card_updated', 'card_deleted'].includes(msg.type)) return;
		if (reloadTimer) clearTimeout(reloadTimer);
		reloadTimer = setTimeout(load, 500);
	});

	onMount(() => {
		load();
		return () => reloadTimer && clearTimeout(reloadTimer);
	});
</script>

<div class="-m-4 flex h-[calc(100%+2rem)] overflow-hidden">
	<div class="flex min-w-0 flex-1 flex-col">
		{#if error}
			<div class="mx-4 mt-3 rounded-lg bg-rose-500/15 px-3 py-2 text-laya-secondary text-rose-300">{error}</div>
		{/if}

		<div class="flex items-center justify-end gap-1 px-4 pt-3">
			{#each [['detailed', 'Detailed'], ['compact', 'Compact']] as [id, label] (id)}
				<button
					type="button"
					onclick={() => setView(id as BoardView)}
					class="rounded-md px-2 py-0.5 text-laya-micro font-medium transition-colors
						{view === id ? 'bg-laya-orange/10 text-laya-orange' : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200'}"
				>{label}</button>
			{/each}
		</div>

		<div class="flex min-h-0 flex-1 gap-3 overflow-x-auto p-4 pt-2">
			{#each COLUMNS as column (column.id)}
				<section
					class="flex {view === 'detailed' ? 'w-[340px] min-w-[300px]' : 'w-72 min-w-[16rem]'} flex-1 flex-col rounded-xl border bg-surface-900/40 transition-colors
						{dropTarget === column.id ? 'border-laya-orange/60 bg-laya-orange/5' : 'border-surface-800'}"
					ondragover={(e) => { e.preventDefault(); dropTarget = column.id; }}
					ondragleave={() => { if (dropTarget === column.id) dropTarget = null; }}
					ondrop={(e) => onDrop(e, column.id)}
					aria-label={column.title}
				>
					<header class="flex items-center gap-2 px-3 pb-2 pt-3">
						<h2 class="text-laya-secondary font-semibold text-surface-200">{column.title}</h2>
						<span class="text-laya-micro text-surface-500">{columns[column.id].length}</span>
						{#if column.id === 'done' && columns.done.length > 0}
							<button
								type="button"
								onclick={archiveAllDone}
								disabled={archivingAll}
								class="ml-auto rounded-md px-2 py-0.5 text-laya-micro font-medium text-surface-400 transition-colors hover:bg-surface-800 hover:text-surface-200 disabled:opacity-50"
							>{archivingAll ? 'Archiving…' : 'Archive all'}</button>
						{/if}
					</header>

					<div class="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-2 pb-3">
						{#if loading}
							<div class="h-16 animate-pulse rounded-lg bg-surface-800/50"></div>
						{:else}
							{#each columns[column.id] as card (card.card_id)}
								{@const platform = platformKey(card.entity_id ?? undefined)}
								{#if view === 'detailed'}
									<!-- The Pulse card itself: summary, actor, source, quick actions. -->
									<div
										role="listitem"
										draggable="true"
										ondragstart={(e) => onDragStart(e, card)}
										ondragend={() => { dragging = null; dropTarget = null; }}
										class="cursor-grab active:cursor-grabbing {dragging?.card_id === card.card_id ? 'opacity-40' : ''}"
									>
										<ActionCardComponent
											{card}
											onselect={openCard}
											selectedCardId={selected?.card_id ?? ''}
											hasSelection={!!selected}
										/>
									</div>
								{:else}
								<button
									type="button"
									draggable="true"
									ondragstart={(e) => onDragStart(e, card)}
									ondragend={() => { dragging = null; dropTarget = null; }}
									onclick={() => openCard(card)}
									class="group w-full rounded-lg border px-3 py-2 text-left transition-colors
										{selected?.card_id === card.card_id
											? 'border-laya-orange/60 bg-surface-800'
											: 'border-surface-800 bg-surface-800/50 hover:border-surface-600 hover:bg-surface-800'}
										{dragging?.card_id === card.card_id ? 'opacity-40' : ''}"
								>
									<div class="mb-1 flex items-center gap-1.5">
										<span class="rounded px-1 text-[9px] font-semibold {PRIORITY_COLORS[card.priority] ?? ''}">{PRIORITY_LABELS[card.priority] ?? card.priority}</span>
										<span class="h-1.5 w-1.5 shrink-0 rounded-full" style="background:{platformDotColor(platform)}"></span>
										<span class="truncate text-laya-micro text-surface-400">{card.source_context ?? platformLabel(platform)}</span>
										{#if AGENT_STATUSES.has(card.status)}
											<span class="ml-auto shrink-0 text-laya-micro text-laya-orange">agent</span>
										{:else}
											<span class="ml-auto shrink-0 text-laya-micro text-surface-500" title={activity(card)}>{timeAgo(activity(card))}</span>
										{/if}
										{#if card.status === 'done'}
											<span
												role="button"
												tabindex="0"
												aria-label="Archive"
												title="Archive"
												onclick={(e) => { e.stopPropagation(); moveTo(card, 'archive'); }}
												onkeydown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); moveTo(card, 'archive'); } }}
												class="shrink-0 rounded p-0.5 text-surface-500 hover:bg-surface-700 hover:text-surface-200"
											>
												<svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 7l-1 12a2 2 0 01-2 2H7a2 2 0 01-2-2L4 7m16 0H4m16 0l-1-3H5L4 7m6 4h4" />
												</svg>
											</span>
										{/if}
									</div>
									<p class="line-clamp-2 text-laya-secondary text-surface-100 {card.status === 'done' ? 'text-surface-400 line-through decoration-surface-600' : ''}">{card.header}</p>
									{#if card.actor_name}
										<p class="mt-1 truncate text-laya-micro text-surface-500">{card.actor_name}</p>
									{/if}
								</button>
								{/if}
							{:else}
								<p class="px-2 py-6 text-center text-laya-micro text-surface-600">
									{column.id === 'backlog' ? 'Nothing new' : 'Drop cards here'}
								</p>
							{/each}
						{/if}
					</div>
				</section>
			{/each}

			<!-- Archive: drop a card here and it leaves the board. -->
			<section
				class="flex w-28 shrink-0 flex-col items-center justify-center gap-2 rounded-xl border border-dashed text-laya-micro transition-colors
					{dropTarget === 'archive' ? 'border-laya-orange/60 bg-laya-orange/5 text-laya-orange' : 'border-surface-700 text-surface-500'}"
				ondragover={(e) => { e.preventDefault(); dropTarget = 'archive'; }}
				ondragleave={() => { if (dropTarget === 'archive') dropTarget = null; }}
				ondrop={(e) => onDrop(e, 'archive')}
				aria-label="Archive"
			>
				<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 7l-1 12a2 2 0 01-2 2H7a2 2 0 01-2-2L4 7m16 0H4m16 0l-1-3H5L4 7m6 4h4" />
				</svg>
				<span>Archive</span>
			</section>
		</div>
	</div>

	{#if selected}
		<aside class="w-[440px] shrink-0 overflow-y-auto border-l border-surface-800 p-3">
			<CardDetail
				card={selected}
				onclose={() => (selected = null)}
				ondismiss={() => { selected = null; load(); }}
				onrunagent={selected.entity_id ? runAgent : undefined}
			/>
		</aside>
	{/if}
</div>
