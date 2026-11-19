// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import { externalLinkTarget } from './externalLinks';

const APP = 'http://localhost:5173';

/** A click target whose closest('a[href]') resolves to an anchor with this href (or none). */
function targetInLink(href: string | null) {
	return {
		closest: (selector: string) =>
			selector === 'a[href]' && href !== null ? ({ href } as HTMLAnchorElement) : null
	} as unknown as EventTarget;
}

describe('externalLinkTarget', () => {
	it('returns the URL of an external https link', () => {
		const url = 'https://acme.slack.com/archives/C123/p1700000000000100';
		expect(externalLinkTarget(targetInLink(url), APP)).toBe(url);
	});

	it('accepts plain http links', () => {
		expect(externalLinkTarget(targetInLink('http://example.com/a'), APP)).toBe('http://example.com/a');
	});

	it('ignores links to the app itself', () => {
		expect(externalLinkTarget(targetInLink(`${APP}/feed`), APP)).toBeNull();
	});

	it('ignores non-http schemes', () => {
		expect(externalLinkTarget(targetInLink('mailto:someone@example.com'), APP)).toBeNull();
		expect(externalLinkTarget(targetInLink('javascript:void(0)'), APP)).toBeNull();
	});

	it('ignores clicks outside a link', () => {
		expect(externalLinkTarget(targetInLink(null), APP)).toBeNull();
		expect(externalLinkTarget(null, APP)).toBeNull();
		expect(externalLinkTarget({} as EventTarget, APP)).toBeNull();
	});
});
