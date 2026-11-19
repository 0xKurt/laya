// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { writable, get } from 'svelte/store';
import { engineApi } from '$lib/api/engine';
import type { Settings } from '$lib/api/types';
import { normalizeHidden, setVisible, type ViewId } from './views';

/** Hidden views, loaded from engine settings (`views.hidden`). */
export const hiddenViews = writable<ViewId[]>([]);

let loading: Promise<ViewId[]> | null = null;

/** Load the hidden list from engine settings. Concurrent callers share one request. */
export function loadHiddenViews(): Promise<ViewId[]> {
	loading ??= engineApi
		.getSettings()
		.then((s) => {
			const hidden = normalizeHidden(s.views?.hidden);
			hiddenViews.set(hidden);
			return hidden;
		})
		.catch(() => get(hiddenViews))
		.finally(() => {
			loading = null;
		});
	return loading;
}

/**
 * Show or hide one view and save it. Omni's visibility also drives
 * `omni.enabled`, so its background synthesis stops while Omni is hidden.
 */
export async function saveViewVisible(id: ViewId, visible: boolean): Promise<void> {
	const previous = get(hiddenViews);
	const next = setVisible(previous, id, visible);
	if (next.length === previous.length && next.every((v, i) => v === previous[i])) return;
	hiddenViews.set(next);
	try {
		const body: Partial<Settings> = { views: { hidden: next } };
		if (id === 'omni') {
			// PUT /settings merges one level deep, so send the whole omni object.
			const { omni } = await engineApi.getSettings();
			if (omni) body.omni = { ...omni, enabled: visible };
		}
		await engineApi.updateSettings(body);
	} catch (e) {
		hiddenViews.set(previous);
		throw e;
	}
}
