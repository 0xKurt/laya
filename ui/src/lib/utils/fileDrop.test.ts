// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi } from 'vitest';
import { carriesFiles, createNativeDragHandler } from './fileDrop';

function handlers() {
	return { onOver: vi.fn(), onLeave: vi.fn(), onPaths: vi.fn() };
}

describe('carriesFiles', () => {
	it('is true for a drag from the OS carrying files', () => {
		expect(carriesFiles({ dataTransfer: { types: ['Files'] } as unknown as DataTransfer })).toBe(true);
	});

	it('is false for an in-app element drag', () => {
		expect(carriesFiles({ dataTransfer: { types: ['text/plain'] } as unknown as DataTransfer })).toBe(false);
		expect(carriesFiles({ dataTransfer: null })).toBe(false);
	});
});

describe('createNativeDragHandler', () => {
	it('reports a drag that entered with file paths, then its drop', () => {
		const h = handlers();
		const on = createNativeDragHandler(h);
		on({ type: 'enter', paths: ['/tmp/a.pdf'] });
		on({ type: 'over' });
		on({ type: 'drop', paths: ['/tmp/a.pdf'] });
		expect(h.onOver).toHaveBeenCalledTimes(2);
		expect(h.onLeave).toHaveBeenCalledTimes(1);
		expect(h.onPaths).toHaveBeenCalledWith(['/tmp/a.pdf']);
	});

	it('ignores an in-page drag that entered without paths', () => {
		const h = handlers();
		const on = createNativeDragHandler(h);
		on({ type: 'enter', paths: [] });
		on({ type: 'over' });
		on({ type: 'leave' });
		on({ type: 'drop', paths: [] });
		expect(h.onOver).not.toHaveBeenCalled();
		expect(h.onLeave).not.toHaveBeenCalled();
		expect(h.onPaths).not.toHaveBeenCalled();
	});

	it('ends a file drag on leave and ignores what follows until the next file enter', () => {
		const h = handlers();
		const on = createNativeDragHandler(h);
		on({ type: 'enter', paths: ['/tmp/a.pdf'] });
		on({ type: 'leave' });
		on({ type: 'over' });
		expect(h.onOver).toHaveBeenCalledTimes(1);
		expect(h.onLeave).toHaveBeenCalledTimes(1);
	});
});
