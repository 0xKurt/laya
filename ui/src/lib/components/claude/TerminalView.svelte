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
		oninput
	}: {
		sessionId: string;
		visible: boolean;
		/** Called once, when the first output arrives. */
		onstarted?: () => void;
		onexit?: (code: number | null) => void;
		/** Called when the user types into this view. */
		oninput?: () => void;
	} = $props();

	let el: HTMLDivElement | undefined = $state();
	let term: Terminal | null = $state(null);
	let fit: FitAddon | null = null;
	let ws: WebSocket | null = null;
	let started = false;
	let disposed = false;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	const encoder = new TextEncoder();

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

	onMount(() => {
		let resizeObserver: ResizeObserver | null = null;
		let themeObserver: MutationObserver | null = null;

		(async () => {
			const [{ Terminal }, { FitAddon }] = await Promise.all([
				import('@xterm/xterm'),
				import('@xterm/addon-fit')
			]);
			if (disposed || !el) return;
			const t = new Terminal({
				fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() || 'monospace',
				fontSize: 13,
				cursorBlink: true,
				scrollback: 5000,
				macOptionIsMeta: true,
				theme: terminalTheme()
			});
			fit = new FitAddon();
			t.loadAddon(fit);
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
		if (visible && term) {
			const t = term;
			requestAnimationFrame(() => {
				fitNow();
				t.focus();
			});
		}
	});
</script>

<div class="h-full w-full" class:hidden={!visible}>
	<div bind:this={el} class="h-full w-full"></div>
</div>
