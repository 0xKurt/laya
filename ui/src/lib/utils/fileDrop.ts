// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Window-level file drop subscription shared by the Run Agent modal and the
// app-wide "Add to Corpus" drop. Two mechanisms are wired at once:
//
// - HTML5 dragover/drop/dragleave on window: the browser and Vite-dev path,
//   which yields File blobs.
// - Tauri's native onDragDropEvent: on macOS the WKWebView does not deliver
//   OS file drops to the DOM, so Tauri reports them with absolute paths.
//
// The subscriber gets whichever fires; inside Tauri it is normally the path
// event (the engine then reads the file in place).

export interface FileDropHandlers {
	onOver?: () => void;
	onLeave?: () => void;
	/** HTML5 drop with File blobs. */
	onFiles?: (files: File[]) => void;
	/** Tauri native drop with absolute paths. */
	onPaths?: (paths: string[]) => void;
}

/**
 * True when an HTML5 drag carries files from the OS. Drags of elements inside
 * the app (a card on the board) carry no 'Files' type and must not raise the
 * drop overlay, which would cover their drop target.
 */
export function carriesFiles(e: Pick<DragEvent, 'dataTransfer'>): boolean {
	const types = e.dataTransfer?.types;
	return !!types && Array.from(types).includes('Files');
}

/** The payload shapes of Tauri's native drag-drop event that the tracker reads. */
export type NativeDragPayload =
	| { type: 'enter'; paths: string[] }
	| { type: 'over' }
	| { type: 'drop'; paths: string[] }
	| { type: 'leave' };

/**
 * Handler for Tauri's native drag-drop events. The native events also fire
 * for drags inside the page; only a drag whose 'enter' carried file paths is
 * a file drag, and its 'over'/'drop'/'leave' are the only ones reported.
 */
export function createNativeDragHandler(h: FileDropHandlers): (p: NativeDragPayload) => void {
	let fileDrag = false;
	return (p) => {
		if (p.type === 'enter') {
			fileDrag = p.paths.length > 0;
			if (fileDrag) h.onOver?.();
		} else if (p.type === 'over') {
			if (fileDrag) h.onOver?.();
		} else if (p.type === 'drop') {
			const wasFileDrag = fileDrag;
			fileDrag = false;
			if (!wasFileDrag) return;
			h.onLeave?.();
			if (p.paths.length > 0) h.onPaths?.(p.paths);
		} else {
			if (fileDrag) h.onLeave?.();
			fileDrag = false;
		}
	};
}

/** Register the drop listeners; returns a combined unsubscribe. */
export function subscribeFileDrop(h: FileDropHandlers): () => void {
	function handleDrop(e: DragEvent) {
		if (!carriesFiles(e)) return;
		e.preventDefault();
		e.stopPropagation();
		h.onLeave?.();
		const dropped = e.dataTransfer?.files;
		if (dropped && dropped.length > 0) h.onFiles?.(Array.from(dropped));
	}

	function handleDragOver(e: DragEvent) {
		if (!carriesFiles(e)) return;
		e.preventDefault();
		e.stopPropagation();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
		h.onOver?.();
	}

	function handleDragLeave(e: DragEvent) {
		if (!carriesFiles(e)) return;
		// Only when leaving the window, not when moving between child elements.
		if (e.relatedTarget === null) h.onLeave?.();
	}

	window.addEventListener('dragover', handleDragOver);
	window.addEventListener('drop', handleDrop);
	window.addEventListener('dragleave', handleDragLeave);

	let unlistenTauri: (() => void) | undefined;
	let disposed = false;
	(async () => {
		try {
			const { getCurrentWebviewWindow } = await import('@tauri-apps/api/webviewWindow');
			const w = getCurrentWebviewWindow();
			const onNative = createNativeDragHandler(h);
			const unlisten = await w.onDragDropEvent((event) => onNative(event.payload));
			// The subscriber may have gone away while the import resolved.
			if (disposed) unlisten();
			else unlistenTauri = unlisten;
		} catch {
			// Not inside Tauri: the HTML5 handlers above cover the browser.
		}
	})();

	return () => {
		disposed = true;
		window.removeEventListener('dragover', handleDragOver);
		window.removeEventListener('drop', handleDrop);
		window.removeEventListener('dragleave', handleDragLeave);
		unlistenTauri?.();
	};
}

/** True when running inside the Tauri shell (native drops deliver paths). */
export function isTauri(): boolean {
	return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/**
 * Open the native file picker (Tauri dialog plugin) and return absolute paths.
 * Returns null outside Tauri or when the user cancels; the caller falls back
 * to an `<input type="file">` in the browser.
 */
export async function pickFilePaths(extensions: string[]): Promise<string[] | null> {
	if (!isTauri()) return null;
	try {
		const { invoke } = await import('@tauri-apps/api/core');
		const result = await invoke<string | string[] | null>('plugin:dialog|open', {
			options: {
				multiple: true,
				directory: false,
				filters: extensions.length ? [{ name: 'Documents', extensions }] : []
			}
		});
		if (!result) return null;
		return Array.isArray(result) ? result : [result];
	} catch {
		return null;
	}
}
