// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// External links open in the user's default browser, never inside the app's
// webview. A page such as a Slack permalink loaded in the webview tries to hand
// off to its desktop app and breaks there; in the browser that hand-off works.

/** The http(s) URL a click should open externally, or null when the click is not on an external link. */
export function externalLinkTarget(target: EventTarget | null, appOrigin: string): string | null {
	const el = target as { closest?: (selector: string) => Element | null } | null;
	const anchor = el?.closest?.('a[href]') as HTMLAnchorElement | null | undefined;
	if (!anchor) return null;
	const href = anchor.href;
	if (!/^https?:/i.test(href)) return null;
	try {
		if (new URL(href).origin === appOrigin) return null;
	} catch {
		return null;
	}
	return href;
}

/** Open a URL in the default browser (Tauri shell), falling back to a new window outside Tauri. */
export async function openExternal(url: string): Promise<void> {
	try {
		const { open } = await import('@tauri-apps/plugin-shell');
		await open(url);
	} catch {
		window.open(url, '_blank', 'noopener,noreferrer');
	}
}

/**
 * Route every click on an external link to the default browser. Registered in
 * the capture phase so it runs before any component handler can let the
 * webview navigate. Returns the unsubscribe.
 */
export function interceptExternalLinks(doc: Document = document): () => void {
	const onClick = (e: MouseEvent) => {
		if (e.defaultPrevented || e.button !== 0) return;
		const url = externalLinkTarget(e.target, window.location.origin);
		if (!url) return;
		e.preventDefault();
		void openExternal(url);
	};
	doc.addEventListener('click', onClick, true);
	return () => doc.removeEventListener('click', onClick, true);
}
