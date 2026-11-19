// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * xterm.js colours from the app's theme tokens (`--color-surface-*`,
 * `--color-laya-orange` in app.css). The tokens are oklch(); xterm expects
 * hex or rgb, so each one is resolved by drawing it on a 1×1 canvas.
 */

import type { ITheme } from '@xterm/xterm';

let ctx: CanvasRenderingContext2D | null = null;

function toHex(cssColor: string, fallback: string): string {
	if (!cssColor) return fallback;
	try {
		ctx ??= document.createElement('canvas').getContext('2d', { willReadFrequently: true });
		if (!ctx) return fallback;
		ctx.clearRect(0, 0, 1, 1);
		ctx.fillStyle = fallback;
		ctx.fillStyle = cssColor;
		ctx.fillRect(0, 0, 1, 1);
		const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
		return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
	} catch {
		return fallback;
	}
}

function token(name: string, fallback: string): string {
	const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
	return toHex(value, fallback);
}

export function terminalTheme(): ITheme {
	if (typeof document === 'undefined') return {};
	const background = token('--color-surface-950', '#141110');
	const foreground = token('--color-surface-100', '#ebe6e1');
	const accent = token('--color-laya-orange', '#f6ac69');
	return {
		background,
		foreground,
		cursor: accent,
		cursorAccent: background,
		selectionBackground: accent + '55'
	};
}
