<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { onMount } from 'svelte';
	import '@xterm/xterm/css/xterm.css';
	import type { Terminal } from '@xterm/xterm';
	import type { FitAddon } from '@xterm/addon-fit';
	import { claudeTerminalWsUrl } from '$lib/api/engine';
	import { waitForEngineToken } from '$lib/config';
	import { engineWsProtocols } from '$lib/stores/websocket';
	import { terminalTheme } from '$lib/claude/terminalTheme';
	import { openExternal } from '$lib/utils/externalLinks';

	/**
	 * One xterm.js view attached to an engine terminal session. The process runs
	 * in the engine; this view attaches over the PTY socket, and the engine
	 * replays the recent output on every attach, so unmounting (navigating away)
	 * and mounting again restores the screen.
	 */
	let {
		sessionId,
		visible,
		onstarted,
		onexit,
		oninput,
		fontSize = 13,
		focusSignal = 0,
		onmissing
	}: {
		sessionId: string;
		visible: boolean;
		/** Font size in px (the side panel uses 12). */
		fontSize?: number;
		/** Changing this number focuses the terminal. */
		focusSignal?: number;
		/** Called when the engine no longer has the session (e.g. after an engine restart). */
		onmissing?: () => void;
		/** Called once, when the first output arrives. */
		onstarted?: () => void;
		onexit?: (code: number | null) => void;
		/** Called when the user types into this view. */
		oninput?: () => void;
	} = $props();

	let el: HTMLDivElement | undefined = $state();
	let wrap: HTMLDivElement | undefined = $state();
	let term: Terminal | null = $state(null);
	let fit: FitAddon | null = null;
	let ws: WebSocket | null = null;
	let started = false;
	let disposed = false;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	const encoder = new TextEncoder();
	// Right-click menu offered when the terminal has a selection.
	let copyMenu = $state<{ x: number; y: number } | null>(null);

	// App shortcuts listen on the document. Keys with Ctrl/Cmd typed into the
	// terminal belong to the program in it (claude's Ctrl+O, Ctrl+D, …), so they
	// stop here. Cmd+P/O/S/D would otherwise open the browser's print/open/
	// save/bookmark dialog in a plain browser; xterm itself does not send Cmd.
	function onKeydown(e: KeyboardEvent) {
		if (!(e.ctrlKey || e.metaKey)) return;
		e.stopPropagation();
		if (e.metaKey && !e.ctrlKey && ['p', 'o', 's', 'd'].includes(e.key.toLowerCase())) e.preventDefault();
	}

	function onContextmenu(e: MouseEvent) {
		if (!term?.hasSelection()) return;
		e.preventDefault();
		e.stopPropagation();
		const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
		copyMenu = { x: e.clientX - box.left, y: e.clientY - box.top };
	}

	async function copySelection() {
		const text = term?.getSelection() ?? '';
		copyMenu = null;
		if (!text) return;
		try {
			await navigator.clipboard.writeText(text);
		} catch {
			// Clipboard API unavailable: fall back to the legacy copy command.
			const ta = document.createElement('textarea');
			ta.value = text;
			document.body.appendChild(ta);
			ta.select();
			document.execCommand('copy');
			ta.remove();
		}
		term?.focus();
	}

	function sendBytes(data: Uint8Array) {
		if (ws?.readyState === WebSocket.OPEN) ws.send(data);
	}

	function sendResize() {
		if (!term || ws?.readyState !== WebSocket.OPEN) return;
		ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
	}

	function fitNow() {
		// Fitting a hidden element measures zero and would shrink the PTY.
		if (!fit || !el || el.offsetParent === null) return;
		try {
			fit.fit();
		} catch {
			// The renderer is not ready yet; the next resize fits again.
		}
	}

	async function connect() {
		if (disposed) return;
		let token: string;
		try {
			token = await waitForEngineToken();
		} catch {
			scheduleReconnect();
			return;
		}
		if (disposed) return;
		const socket = new WebSocket(claudeTerminalWsUrl(sessionId), engineWsProtocols(token));
		socket.binaryType = 'arraybuffer';
		ws = socket;
		socket.onopen = () => {
			// The replayed output is drawn for the PTY's size; fit first so the
			// resize below makes claude redraw for this view.
			term?.reset();
			fitNow();
			sendResize();
		};
		socket.onmessage = (event) => {
			if (event.data instanceof ArrayBuffer) {
				term?.write(new Uint8Array(event.data));
				if (!started) {
					started = true;
					onstarted?.();
				}
				return;
			}
			try {
				const msg = JSON.parse(event.data as string);
				if (msg.type === 'exit') {
					term?.write(`\r\n\x1b[2m[claude exited${msg.code != null ? ` with code ${msg.code}` : ''}]\x1b[0m\r\n`);
					onexit?.(msg.code ?? null);
				} else if (msg.type === 'status' && !msg.running) {
					onexit?.(msg.exit_code ?? null);
				}
			} catch {
				// Not a control message.
			}
		};
		socket.onclose = (event) => {
			if (ws === socket) ws = null;
			// 1000: the session was closed; 4404: it no longer exists.
			if (event.code === 4404 && !disposed) onmissing?.();
			if (disposed || event.code === 1000 || event.code === 4404) return;
			scheduleReconnect();
		};
	}

	function scheduleReconnect() {
		if (disposed || reconnectTimer) return;
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			connect();
		}, 1500);
	}

	// Native listeners (not delegated) so stopPropagation runs before the
	// document-level shortcut handlers see the event.
	$effect(() => {
		const node = wrap;
		if (!node) return;
		node.addEventListener('keydown', onKeydown);
		node.addEventListener('contextmenu', onContextmenu);
		return () => {
			node.removeEventListener('keydown', onKeydown);
			node.removeEventListener('contextmenu', onContextmenu);
		};
	});

	onMount(() => {
		let resizeObserver: ResizeObserver | null = null;
		let themeObserver: MutationObserver | null = null;

		(async () => {
			const [{ Terminal }, { FitAddon }, { WebLinksAddon }] = await Promise.all([
				import('@xterm/xterm'),
				import('@xterm/addon-fit'),
				import('@xterm/addon-web-links')
			]);
			if (disposed || !el) return;
			const t = new Terminal({
				fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() || 'monospace',
				fontSize,
				cursorBlink: true,
				scrollback: 5000,
				macOptionIsMeta: true,
				// OSC 8 hyperlinks (codex prints them) open the same way as plain URLs.
				linkHandler: {
					activate: (event, uri) => {
						event.preventDefault();
						if (/^https?:/i.test(uri)) void openExternal(uri);
					}
				},
				theme: terminalTheme()
			});
			fit = new FitAddon();
			t.loadAddon(fit);
			// URLs printed by the agent open in the default browser (same opener as app links).
			t.loadAddon(
				new WebLinksAddon((event, uri) => {
					event.preventDefault();
					void openExternal(uri);
				})
			);
			t.open(el);
			term = t;
			fitNow();
			t.onData((data) => {
				sendBytes(encoder.encode(data));
				oninput?.();
			});
			t.onBinary((data) => sendBytes(Uint8Array.from(data, (c) => c.charCodeAt(0) & 0xff)));
			t.onResize(() => sendResize());
			resizeObserver = new ResizeObserver(() => fitNow());
			resizeObserver.observe(el);
			themeObserver = new MutationObserver(() => {
				if (term) term.options.theme = terminalTheme();
			});
			themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
			connect();
		})();

		return () => {
			disposed = true;
			if (reconnectTimer) clearTimeout(reconnectTimer);
			resizeObserver?.disconnect();
			themeObserver?.disconnect();
			ws?.close();
			ws = null;
			term?.dispose();
			term = null;
		};
	});

	$effect(() => {
		if (term && term.options.fontSize !== fontSize) {
			term.options.fontSize = fontSize;
			fitNow();
		}
	});

	$effect(() => {
		if (focusSignal && term) term.focus();
	});

	$effect(() => {
		if (visible && term) {
			const t = term;
			requestAnimationFrame(() => {
				fitNow();
				t.focus();
			});
		}
	});
</script>

<div bind:this={wrap} class="relative h-full w-full" class:hidden={!visible}>
	<div bind:this={el} class="h-full w-full" data-terminal-session={sessionId}></div>
	{#if copyMenu}
		<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
		<div class="fixed inset-0 z-40" onclick={() => (copyMenu = null)} oncontextmenu={(e) => { e.preventDefault(); copyMenu = null; }}></div>
		<div class="absolute z-50 min-w-[7rem] rounded-md border border-surface-700 bg-surface-900 py-1 shadow-xl" style="left: {copyMenu.x}px; top: {copyMenu.y}px;">
			<button type="button" class="block w-full px-3 py-1 text-left text-laya-micro text-surface-200 hover:bg-surface-800" onclick={copySelection}>Copy</button>
		</div>
	{/if}
</div>
