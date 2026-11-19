<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	A small floating panel for My work row actions (menus, comment box, results).
	It is portalled to <body> with fixed coordinates, so the scrolling list it
	belongs to does not clip it. Closes on Escape and on a click outside both
	the panel and its anchor.
-->
<script lang="ts">
	import type { Snippet } from 'svelte';
	import { portal } from '$lib/actions/portal';

	interface Props {
		anchor: HTMLElement | null;
		onclose: () => void;
		width?: number;
		label: string;
		children: Snippet;
	}

	let { anchor, onclose, width = 240, label, children }: Props = $props();

	let panel = $state<HTMLDivElement | null>(null);
	let pos = $state({ top: 0, left: 0, openUp: false });

	function place() {
		if (!anchor) return;
		const r = anchor.getBoundingClientRect();
		const below = window.innerHeight - r.bottom;
		const openUp = below < 260 && r.top > below;
		const left = Math.min(Math.max(8, r.right - width), window.innerWidth - width - 8);
		pos = { top: openUp ? r.top - 4 : r.bottom + 4, left, openUp };
	}

	$effect(() => {
		place();
		const onDown = (e: MouseEvent) => {
			const t = e.target as Node;
			if (panel?.contains(t) || anchor?.contains(t)) return;
			onclose();
		};
		const onKey = (e: KeyboardEvent) => {
			if (e.key === 'Escape') onclose();
		};
		// Scrolling the list moves the anchor away from the fixed panel.
		const onScroll = (e: Event) => {
			if (panel && e.target instanceof Node && panel.contains(e.target)) return;
			onclose();
		};
		window.addEventListener('mousedown', onDown, true);
		window.addEventListener('keydown', onKey);
		window.addEventListener('scroll', onScroll, true);
		window.addEventListener('resize', onclose);
		return () => {
			window.removeEventListener('mousedown', onDown, true);
			window.removeEventListener('keydown', onKey);
			window.removeEventListener('scroll', onScroll, true);
			window.removeEventListener('resize', onclose);
		};
	});
</script>

<div
	use:portal
	bind:this={panel}
	role="dialog"
	aria-label={label}
	class="fixed z-[60] rounded-lg border border-surface-700 bg-surface-900 p-1 text-laya-secondary shadow-xl"
	style="left: {pos.left}px; top: {pos.top}px; width: {width}px; {pos.openUp ? 'transform: translateY(-100%);' : ''}"
>
	{@render children()}
</div>
