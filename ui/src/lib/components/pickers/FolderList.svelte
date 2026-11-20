<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
	A list of folders: "Choose folder…" opens the native folder dialog in the
	desktop app (the shell's `pick_folder` command); the browser build types a
	path instead, which the engine checks exists (GET /claude-code/folders/check).
	Each row has a remove button.
-->
<script lang="ts">
	import { checkFolder } from '$lib/api/workScope';
	import { isTauri } from '$lib/utils/fileDrop';
	import { addFolder } from '$lib/utils/pickers';

	let {
		folders = $bindable([]),
		id,
		dialogTitle = 'Choose a folder',
		emptyText = 'No folder added.',
		disabled = false,
		onchange
	}: {
		folders?: string[];
		id: string;
		dialogTitle?: string;
		emptyText?: string;
		disabled?: boolean;
		onchange?: (folders: string[]) => void;
	} = $props();

	const desktop = isTauri();
	let typed = $state('');
	let checking = $state(false);
	let error = $state<string | null>(null);

	function set(next: string[]) {
		if (next === folders) return;
		folders = next;
		onchange?.(next);
	}

	async function choose() {
		error = null;
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			const path = await invoke<string>('pick_folder', { title: dialogTitle });
			if (path) {
				if (folders.includes(path)) error = `${path} is already in the list.`;
				else set(addFolder(folders, path));
			}
		} catch (e) {
			const msg = String(e ?? '');
			if (msg !== 'cancelled') error = msg || 'Could not open the folder dialog';
		}
	}

	async function addTyped() {
		const text = typed.trim();
		if (!text) return;
		checking = true;
		error = null;
		try {
			const r = await checkFolder(text);
			if (!r.exists) {
				error = r.error ?? 'This folder does not exist.';
			} else if (folders.includes(r.path)) {
				error = `${r.path} is already in the list.`;
			} else {
				set(addFolder(folders, r.path));
				typed = '';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not check the folder';
		} finally {
			checking = false;
		}
	}
</script>

<div class="flex flex-col gap-2">
	{#if folders.length === 0}
		<p class="text-laya-secondary text-surface-500">{emptyText}</p>
	{:else}
		<ul class="flex flex-col gap-1" aria-label="Folders">
			{#each folders as f (f)}
				<li class="flex items-center gap-2 rounded-md border border-surface-700 bg-surface-900/60 px-3 py-1.5">
					<svg class="h-4 w-4 shrink-0 text-surface-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
						<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" stroke-linejoin="round" />
					</svg>
					<span class="min-w-0 flex-1 truncate font-mono text-laya-secondary text-surface-200" title={f}>{f}</span>
					<button
						type="button"
						class="rounded px-1.5 text-surface-400 hover:bg-surface-700 hover:text-surface-100 disabled:opacity-50"
						aria-label="Remove {f}"
						title="Remove"
						{disabled}
						onclick={() => set(folders.filter((x) => x !== f))}>×</button
					>
				</li>
			{/each}
		</ul>
	{/if}
	{#if desktop}
		<button
			{id}
			type="button"
			{disabled}
			onclick={choose}
			class="w-fit rounded-md border border-surface-600 px-3 py-1.5 text-laya-secondary text-surface-200 hover:border-laya-orange/60 hover:text-laya-orange disabled:opacity-50"
			>Choose folder…</button
		>
	{:else}
		<div class="flex gap-2">
			<input
				{id}
				bind:value={typed}
				onkeydown={(e) => {
					if (e.key === 'Enter') {
						e.preventDefault();
						addTyped();
					}
				}}
				{disabled}
				placeholder="Folder path, e.g. ~/code"
				aria-label="Folder path"
				class="min-w-0 flex-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-1.5 font-mono text-laya-secondary text-surface-200 focus:border-laya-orange focus:outline-none"
			/>
			<button
				type="button"
				onclick={addTyped}
				disabled={disabled || checking || !typed.trim()}
				class="rounded-md border border-surface-600 px-3 py-1.5 text-laya-secondary text-surface-200 hover:border-laya-orange/60 hover:text-laya-orange disabled:opacity-50"
				>{checking ? 'Checking…' : 'Add folder'}</button
			>
		</div>
	{/if}
	{#if error}<p class="text-laya-secondary text-error-400" role="alert">{error}</p>{/if}
</div>
