import { defineConfig } from "vitest/config";

// The specs exercise the runtime-agnostic events-core package and shared
// webhook pipeline on web-standard APIs (fetch, crypto.subtle, TextEncoder),
// all present in Node 20+. The Cloudflare Worker adapter and its
// miniflare-based spec live in the private deploy repo (repo split), which
// runs this same protocol surface under the workers pool there.
export default defineConfig({
	test: {
		environment: "node",
	},
});
