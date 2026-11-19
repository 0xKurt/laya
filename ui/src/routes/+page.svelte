<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { goto } from '$app/navigation';
	import { landingPath } from '$lib/views/views';
	import { loadHiddenViews } from '$lib/views/store';
	loadHiddenViews().then((hidden) => goto(landingPath(hidden), { replaceState: true }));
</script>
