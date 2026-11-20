// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

/**
 * Chips of your own team entry (Settings → Team): other emails and platform
 * accounts, with the identities the coding agent sources read offered as
 * suggestions.
 */

import { engineApi } from '$lib/api/engine';
import type { ClaudeCodeIdentity, ClaudeCodePlatform } from '$lib/api/types';
import { selfMemberFromIdentities } from './claudeCode';
import type { ChipSuggestion } from './pickers';

const EMAIL = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/;

/** Why `value` cannot be added as another email, or null. */
export function aliasError(value: string, existing: string[]): string | null {
	if (!value) return 'Enter an email address.';
	if (!EMAIL.test(value)) return `"${value}" is not an email address.`;
	if (existing.map((e) => e.toLowerCase()).includes(value.toLowerCase())) return `${value} is already added.`;
	return null;
}

/** Account and email suggestions from the identities of the connected sources. */
export function identitySuggestions(
	identities: Partial<Record<ClaudeCodePlatform, ClaudeCodeIdentity>>
): { accounts: ChipSuggestion[]; aliases: ChipSuggestion[] } {
	const self = selfMemberFromIdentities(identities);
	if (!self) return { accounts: [], aliases: [] };
	const where = (value: string) =>
		(Object.entries(identities) as [ClaudeCodePlatform, ClaudeCodeIdentity][])
			.filter(([, i]) => [i.email, i.slack_user_id, i.github_login].some((v) => v?.toLowerCase() === value.toLowerCase()))
			.map(([p]) => p)
			.join(', ');
	const chip = (value: string): ChipSuggestion => {
		const from = where(value);
		return { value, label: from ? `${value} · ${from}` : value };
	};
	const emails = [self.email, ...self.aliases].filter(Boolean);
	return { accounts: self.accounts.map(chip), aliases: emails.map(chip) };
}

export async function selfAccountSuggestions(): Promise<{ accounts: ChipSuggestion[]; aliases: ChipSuggestion[] }> {
	const status = await engineApi.getClaudeCodeStatus();
	const identities: Partial<Record<ClaudeCodePlatform, ClaudeCodeIdentity>> = {};
	for (const [p, s] of Object.entries(status.platforms) as [ClaudeCodePlatform, { identity?: ClaudeCodeIdentity | null }][]) {
		if (s?.identity) identities[p] = s.identity;
	}
	return identitySuggestions(identities);
}
