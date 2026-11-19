// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/** Pure helpers of the filter rules editor (RulesEditor.svelte). */

import type { SimpleCondition } from '$lib/api/types';

/** Select value of the "Other field…" option, which reveals a free path input. */
export const OTHER_FIELD = '__other__';

/** A dot path into the event, e.g. `content.metadata.github_reason`. */
export function isFieldPath(path: string): boolean {
	return /^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*$/.test(path.trim());
}

/** The form's value text as the condition value: `in` takes a list. */
export function conditionValue(operator: SimpleCondition['operator'], text: string): string | string[] {
	if (operator !== 'in') return text;
	return text
		.split(',')
		.map((v) => v.trim())
		.filter((v) => v !== '');
}

/** A condition value as the form's text. */
export function valueText(value: string | string[]): string {
	return typeof value === 'string' ? value : value.join(', ');
}

/** A condition value for the rule summary line. */
export function valueSummary(value: string | string[]): string {
	return typeof value === 'string' ? `"${value}"` : `[${value.join(', ')}]`;
}

/** Rules the app seeds are named "<Origin> · <title>" (e.g. "Claude Code · Ignore bot comments");
 *  the origin is shown as a badge. */
export function ruleNameParts(name: string): { origin: string | null; title: string } {
	const m = /^(Claude Code) · (.+)$/.exec(name);
	return m ? { origin: m[1], title: m[2] } : { origin: null, title: name };
}
