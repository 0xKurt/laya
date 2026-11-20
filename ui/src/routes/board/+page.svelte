<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { onMount } from 'svelte';
	import { flip } from 'svelte/animate';
	import {
		dndzone,
		setKeyboardDragTrigger,
		SHADOW_ITEM_MARKER_PROPERTY_NAME,
		TRIGGERS,
		type DndEvent
	} from 'svelte-dnd-action';
	import CardDetail from '$lib/components/feed/CardDetail.svelte';
	import ActionCardComponent from '$lib/components/feed/ActionCard.svelte';
	import { engineApi } from '$lib/api/engine';
	import type { ActionCard } from '$lib/api/types';
	import { lastMessage } from '$lib/stores/websocket';
	import { timeAgo } from '$lib/utils/datetime';
	import { formatMinutes } from '$lib/sessions/session';
	import { PRIORITY_COLORS, PRIORITY_LABELS, platformDotColor, platformKey, platformLabel } from '$lib/utils/cardVisuals';
	import {
		LANES,
		EMPTY_FILTERS,
		applyMove,
		groupByLane,
		isAgentSession,
		isFiltering,
		isOrderedLane,
		mergeVisibleOrder,
		planMove,
		presentPlatforms,
		withLaneOrder,
		withoutLaneTags,
		youMinutesByLane,
		type BoardCard,
		type BoardFilters,
		type DropTarget,
		type Lane,
		type LaneTagIds,
		type MoveStep
	} from '$lib/board/board';

	let cards = $state<BoardCard[]>([]);
	let laneTagIds = $state<LaneTagIds | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let selected = $state<ActionCard | null>(null);

	// ── view mode (remembered per device) ──
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
			// storage unavailable: the choice lasts for this session only
		}
	}

	// ── filters (remembered per device; the search text is not) ──
	const FILTERS_KEY = 'laya.board.filters';
	const PRIORITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
	function storedFilters(): BoardFilters {
		try {
			const raw = JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}');
			return {
				search: '',
				priorities: Array.isArray(raw.priorities) ? raw.priorities : [],
				platforms: Array.isArray(raw.platforms) ? raw.platforms : []
			};
		} catch {
			return { ...EMPTY_FILTERS };
		}
	}
	let filters = $state<BoardFilters>(storedFilters());
	$effect(() => {
		const toStore = JSON.stringify({ priorities: filters.priorities, platforms: filters.platforms });
		try {
			localStorage.setItem(FILTERS_KEY, toStore);
		} catch {
			// storage unavailable: filters last for this session only
		}
	});
	const filtering = $derived(isFiltering(filters));

	function toggle(list: string[], value: string): string[] {
		return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
	}

	// Only the sources that have cards on the board get a chip.
	const platforms = $derived(presentPlatforms(cards));
	const columns = $derived(groupByLane(cards, filters));
	const youMinutes = $derived(youMinutesByLane(columns));

	async function load() {
		try {
			const board = await engineApi.getBoard();
			cards = board.cards;
			laneTagIds = board.lane_tags;
			error = null;
			if (selected && !cards.some((c) => c.card_id === selected!.card_id)) selected = null;
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

	async function runStep(card: ActionCard, step: MoveStep, ids: LaneTagIds) {
		switch (step.kind) {
			case 'archive':
				return engineApi.archiveCard(card.card_id);
			case 'done':
				return engineApi.markCardDone(card.card_id);
			case 'reopen':
				return engineApi.reopenCard(card.card_id);
			case 'assign':
				return engineApi.assignTag({ tag_name_or_id: ids[step.lane], target_type: 'card', target_id: card.card_id });
			case 'unassign':
				return engineApi.removeTag({ tag_id: ids[step.lane], target_type: 'card', target_id: card.card_id });
		}
	}

	async function moveTo(card: ActionCard, target: DropTarget) {
		const ids = laneTagIds;
		if (!ids) return;
		const steps = planMove(card, target);
		if (steps.length === 0) return;
		const previous = cards;
		// Optimistic: move first, reload from the engine afterwards.
		cards =
			target === 'archive'
				? cards.filter((c) => c.card_id !== card.card_id)
				: cards.map((c) => (c.card_id === card.card_id ? applyMove(c, target, ids) : c));
		if (target === 'archive' && selected?.card_id === card.card_id) selected = null;
		try {
			for (const step of steps) await runStep(card, step, ids);
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
		const doneIds = new Set(done.map((c) => c.card_id));
		cards = cards.filter((c) => !doneIds.has(c.card_id));
		if (selected && doneIds.has(selected.card_id)) selected = null;
		const results = await Promise.allSettled(done.map((c) => engineApi.archiveCard(c.card_id)));
		const failed = results.filter((r) => r.status === 'rejected').length;
		if (failed) error = `${failed} of ${done.length} cards could not be archived`;
		archivingAll = false;
		await load();
	}

	// Starting a workspace agent moves the card to In progress; the lane tag
	// keeps it there after the agent finishes.
	async function runAgent(entityId: string) {
		const card = selected;
		try {
			await engineApi.runEntityAgent(entityId);
			if (card && laneTagIds) {
				for (const step of planMove(card, 'in_progress')) await runStep(card, step, laneTagIds);
			}
		} catch (e: any) {
			error = e?.body?.detail ?? e?.message ?? 'Failed to start agent';
		}
		await load();
	}

	// ── drag and drop (svelte-dnd-action) ──
	// The library drags with mouse/touch events, not HTML5 drag and drop, which
	// the native file-drop handler of the Tauri window on macOS swallows. It
	// lifts a copy of the card under the pointer, opens a gap where the card
	// will land, animates the other cards aside and scrolls a lane near its
	// edges. A press that moves less than 3px stays a click, so the card still
	// opens and its own buttons still work. Keyboard: focus a card, Space picks
	// it up, arrows move it, Tab moves it to the next lane, Space drops it.
	const FLIP_MS = 180;
	type Zone = DropTarget;
	type Item = BoardCard & { id: string; [SHADOW_ITEM_MARKER_PROPERTY_NAME]?: boolean };
	const ZONES: Zone[] = ['backlog', 'todo', 'in_progress', 'done', 'archive'];
	const toItems = (list: BoardCard[]): Item[] => list.map((c) => ({ ...c, id: c.card_id }));

	// What each zone shows. Derived from the cards except while a drag runs:
	// the library then owns the order (gap, cards moved aside).
	let zones = $state.raw<Record<Zone, Item[]>>({ backlog: [], todo: [], in_progress: [], done: [], archive: [] });
	let dragActive = $state(false);
	let hoverZone = $state<Zone | null>(null);
	$effect(() => {
		const next = columns;
		if (dragActive) return;
		zones = {
			backlog: toItems(next.backlog),
			todo: toItems(next.todo),
			in_progress: toItems(next.in_progress),
			done: toItems(next.done),
			archive: []
		};
	});

	function onConsider(zone: Zone, e: CustomEvent<DndEvent<Item>>) {
		dragActive = true;
		const { trigger } = e.detail.info;
		if (trigger === TRIGGERS.DRAGGED_ENTERED || trigger === TRIGGERS.DRAG_STARTED) hoverZone = zone;
		else if (trigger === TRIGGERS.DRAGGED_LEFT && hoverZone === zone) hoverZone = null;
		else if (trigger === TRIGGERS.DRAGGED_LEFT_ALL) hoverZone = null;
		zones = { ...zones, [zone]: e.detail.items };
	}

	function onFinalize(zone: Zone, e: CustomEvent<DndEvent<Item>>) {
		const items = e.detail.items;
		const id = e.detail.info.id;
		zones = { ...zones, [zone]: items };
		hoverZone = null;
		// The library finalizes both the origin and the target zone; the target
		// is the one that holds the card. The board takes over again once both
		// have run, from the (optimistically updated) cards.
		if (items.some((i) => i.id === id)) dropCard(id, zone, items.map((i) => i.id));
		setTimeout(() => (dragActive = false), 0);
	}

	/** Persist a drop: lane change through the status/tag routes, then the lane order. */
	async function dropCard(cardId: string, zone: Zone, visibleIds: string[]) {
		const ids = laneTagIds;
		const card = cards.find((c) => c.card_id === cardId);
		if (!ids || !card) return;
		if (zone === 'archive') return moveTo(card, 'archive');

		const steps = planMove(card, zone);
		const laneBefore = groupByLane(cards, EMPTY_FILTERS)[zone].map((c) => c.card_id);
		const order = isOrderedLane(zone) ? mergeVisibleOrder(laneBefore.filter((x) => x !== cardId), visibleIds) : [];
		const orderChanged = isOrderedLane(zone) && order.join() !== laneBefore.join();
		if (steps.length === 0 && !orderChanged) return;

		const previous = cards;
		// Optimistic: move first, reload from the engine afterwards.
		const moved = cards.map((c) => (c.card_id === cardId ? { ...applyMove(c, zone, ids), lane: zone, position: null } : c));
		cards = isOrderedLane(zone) ? withLaneOrder(moved, zone, order) : moved;
		try {
			for (const step of steps) await runStep(card, step, ids);
			if (isOrderedLane(zone)) await engineApi.setBoardOrder(zone, order);
		} catch (e: any) {
			cards = previous;
			error = e?.body?.detail ?? e?.message ?? 'Could not move the card';
		}
		await load();
	}

	function zoneOptions(zone: Zone) {
		return {
			items: zones[zone],
			flipDurationMs: FLIP_MS,
			type: 'board-card',
			dropTargetStyle: {},
			// Lane detection follows the pointer, not the centre of a tall card.
			useCursorForDetection: true,
			// Over Archive the card keeps its own look instead of the hidden archive row.
			morphDisabled: zone === 'archive'
		};
	}

	function onItemKeydown(e: KeyboardEvent, card: BoardCard) {
		if (e.key === 'Enter' && e.target === e.currentTarget) openCard(card);
	}

	// Reload on card and tag changes pushed by the engine (new cards, status changes, lane tags).
	let reloadTimer: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const msg = $lastMessage;
		if (!msg || !['card_created', 'card_updated', 'card_deleted', 'tags_changed'].includes(msg.type)) return;
		if (reloadTimer) clearTimeout(reloadTimer);
		reloadTimer = setTimeout(load, 500);
	});

	onMount(() => {
		// Space picks a card up for a keyboard drag; Enter keeps opening it.
		setKeyboardDragTrigger('space');
		load();
		return () => {
			if (reloadTimer) clearTimeout(reloadTimer);
		};
	});
</script>

<div class="-m-4 flex h-[calc(100%+2rem)] overflow-hidden">
	<div class="flex min-w-0 flex-1 flex-col">
		{#if error}
			<div class="mx-4 mt-3 rounded-lg bg-rose-500/15 px-3 py-2 text-laya-secondary text-rose-300">{error}</div>
		{/if}

		<div class="flex flex-wrap items-center gap-1.5 px-4 pt-3">
			<input
				type="search"
				placeholder="Search cards…"
				bind:value={filters.search}
				class="w-56 rounded-md border border-surface-700 bg-surface-900/60 px-2 py-1 text-laya-secondary text-surface-200 placeholder:text-surface-500 focus:border-laya-orange/60 focus:outline-none"
			/>
			<span class="ml-2 text-laya-micro text-surface-500">Priority</span>
			{#each PRIORITIES as p (p)}
				<button
					type="button"
					onclick={() => (filters.priorities = toggle(filters.priorities, p))}
					class="rounded px-1.5 py-0.5 text-[10px] font-semibold transition-opacity
						{PRIORITY_COLORS[p]} {filters.priorities.length && !filters.priorities.includes(p) ? 'opacity-35' : ''}
						{filters.priorities.includes(p) ? 'ring-1 ring-laya-orange/70' : ''}"
				>{PRIORITY_LABELS[p]}</button>
			{/each}
			{#if platforms.length > 1}
				<span class="ml-2 text-laya-micro text-surface-500">Source</span>
				{#each platforms as p (p)}
					<button
						type="button"
						onclick={() => (filters.platforms = toggle(filters.platforms, p))}
						class="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-laya-micro transition-colors
							{filters.platforms.includes(p) ? 'bg-laya-orange/10 text-laya-orange' : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200'}"
					>
						<span class="h-1.5 w-1.5 rounded-full" style="background:{platformDotColor(p)}"></span>
						{platformLabel(p)}
					</button>
				{/each}
			{/if}
			{#if filtering}
				<button
					type="button"
					onclick={() => (filters = { ...EMPTY_FILTERS })}
					class="ml-1 text-laya-micro text-surface-500 underline-offset-2 hover:text-surface-200 hover:underline"
				>Clear</button>
			{/if}
			<span class="flex-1"></span>
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
			{#each LANES as column (column.id)}
				<section
					class="flex {view === 'detailed' ? 'w-[340px] min-w-[300px]' : 'w-72 min-w-[16rem]'} flex-1 flex-col rounded-xl border bg-surface-900/40 transition-colors
						{hoverZone === column.id ? 'border-laya-orange/60 bg-laya-orange/5' : 'border-surface-800'}"
					aria-label={column.title}
				>
					<header class="flex items-center gap-2 px-3 pb-2 pt-3">
						<h2 class="text-laya-secondary font-semibold text-surface-200">{column.title}</h2>
						<span class="text-laya-micro text-surface-500">{columns[column.id].length}</span>
						{#if youMinutes[column.id] > 0}
							<span class="text-laya-micro text-surface-400" title="Your estimated time for the cards in this lane">you ~{formatMinutes(youMinutes[column.id])}</span>
						{/if}
						{#if column.id === 'done'}
							<span class="text-laya-micro text-surface-600">last 7 days</span>
						{/if}
						{#if column.id === 'done' && columns.done.length > 0}
							<button
								type="button"
								onclick={archiveAllDone}
								disabled={archivingAll}
								class="ml-auto rounded-md px-2 py-0.5 text-laya-micro font-medium text-surface-400 transition-colors hover:bg-surface-800 hover:text-surface-200 disabled:opacity-50"
							>{archivingAll ? 'Archiving…' : 'Archive all'}</button>
						{/if}
					</header>

					<div class="relative flex min-h-0 flex-1 flex-col">
						{#if loading}
							<div class="mx-2 h-16 animate-pulse rounded-lg bg-surface-800/50"></div>
						{:else if zones[column.id].length === 0}
							<!-- Outside the drop zone: a zone holds only its cards. -->
							<p class="pointer-events-none absolute inset-x-0 top-0 px-2 py-6 text-center text-laya-micro text-surface-600">
								{filtering ? 'No matching cards' : column.id === 'backlog' ? 'Nothing new' : 'Drop cards here'}
							</p>
						{/if}
						<div
							class="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-2 pb-3"
							aria-label={column.title}
							use:dndzone={zoneOptions(column.id)}
							onconsider={(e) => onConsider(column.id, e)}
							onfinalize={(e) => onFinalize(column.id, e)}
						>
							{#each zones[column.id] as card (card.id)}
								{@const platform = platformKey(card.entity_id)}
								<!-- The library makes each item focusable and draggable from the keyboard; Enter opens the card. -->
								<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
								<div
									role="listitem"
									aria-label={card.header}
									class="relative cursor-grab rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-laya-orange/70"
									animate:flip={{ duration: FLIP_MS }}
									onkeydown={(e) => onItemKeydown(e, card)}
								>
									{#if card[SHADOW_ITEM_MARKER_PROPERTY_NAME]}
										<!-- The gap where the dragged card will land; the library hides the card's own copy here. -->
										<div class="board-gap absolute inset-0 z-10 rounded-lg border-2 border-dashed border-laya-orange/60 bg-laya-orange/5"></div>
									{/if}
									<div class="board-card">
										{#if view === 'detailed'}
											<!-- The Pulse card itself: summary, actor, source, quick actions. -->
											<ActionCardComponent
												card={withoutLaneTags(card)}
												onselect={openCard}
												selectedCardId={selected?.card_id ?? ''}
												hasSelection={!!selected}
											/>
										{:else}
											<button
												type="button"
												tabindex="-1"
												onclick={() => openCard(card)}
												class="group w-full cursor-grab rounded-lg border px-3 py-2 text-left transition-colors
													{selected?.card_id === card.card_id
														? 'border-laya-orange/60 bg-surface-800'
														: 'border-surface-800 bg-surface-800/50 hover:border-surface-600 hover:bg-surface-800'}"
											>
											<div class="mb-1 flex items-center gap-1.5">
												<span class="rounded px-1 text-[9px] font-semibold {PRIORITY_COLORS[card.priority] ?? ''}">{PRIORITY_LABELS[card.priority] ?? card.priority}</span>
												<span class="h-1.5 w-1.5 shrink-0 rounded-full" style="background:{platformDotColor(platform)}"></span>
												<span class="truncate text-laya-micro text-surface-400">{card.source_context ?? platformLabel(platform)}</span>
												{#if isAgentSession(card)}
													<span
														class="ml-auto flex shrink-0 items-center gap-1 rounded bg-violet-500/15 px-1 text-laya-micro text-violet-300"
														title={card.status === 'awaiting_input' ? 'Claude session waiting for input' : 'Claude session running'}
													>
														<span class="h-1.5 w-1.5 rounded-full bg-violet-400 {card.status === 'agent_running' ? 'animate-pulse' : ''}"></span>
														{card.status === 'awaiting_input' ? 'input' : 'agent'}
													</span>
												{:else}
													<span class="ml-auto shrink-0 text-laya-micro text-surface-500" title={card.created_at}>{timeAgo(card.created_at)}</span>
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
									</div>
								</div>
							{/each}
						</div>
					</div>
				</section>
			{/each}

			<!-- Archive: drop a card here and it leaves the board. -->
			<section
				class="relative flex w-28 shrink-0 flex-col items-center justify-center rounded-xl border border-dashed text-laya-micro transition-colors
					{hoverZone === 'archive' ? 'border-laya-orange/60 bg-laya-orange/5 text-laya-orange' : 'border-surface-700 text-surface-500'}"
				aria-label="Archive"
			>
				<div class="pointer-events-none flex flex-col items-center gap-2">
					<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 7l-1 12a2 2 0 01-2 2H7a2 2 0 01-2-2L4 7m16 0H4m16 0l-1-3H5L4 7m6 4h4" />
					</svg>
					<span>Archive</span>
				</div>
				<!-- The whole column is the drop zone; a card over it opens no gap. -->
				<div
					class="absolute inset-0"
					aria-label="Archive"
					use:dndzone={zoneOptions('archive')}
					onconsider={(e) => onConsider('archive', e)}
					onfinalize={(e) => onFinalize('archive', e)}
				>
					{#each zones.archive as item (item.id)}
						<div class="h-0 overflow-hidden" animate:flip={{ duration: FLIP_MS }}></div>
					{/each}
				</div>
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

<style>
	/* The lifted card that follows the pointer (created by svelte-dnd-action). */
	:global(#dnd-action-dragged-el) {
		cursor: grabbing;
	}
	:global(#dnd-action-dragged-el .board-card) {
		transform: rotate(1.5deg) scale(1.03);
		box-shadow:
			0 18px 40px -8px rgb(0 0 0 / 0.55),
			0 0 0 1px var(--color-laya-orange);
		border-radius: 0.5rem;
		transition:
			transform 120ms ease-out,
			box-shadow 120ms ease-out;
	}
	/* The library hides the placeholder copy; its dashed outline stays visible. */
	.board-gap {
		visibility: visible;
	}
</style>
