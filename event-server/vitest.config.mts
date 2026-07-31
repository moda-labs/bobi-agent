import { defineConfig } from "vitest/config";

// The specs exercise the runtime-agnostic events-core package and shared
// webhook pipeline on web-standard APIs (fetch, crypto.subtle, TextEncoder),
// all present in Node 20+.
//
// `include` is scoped to this compile unit's own test/ directory on purpose.
// The Cloudflare Worker adapter is a sibling workspace (worker/) whose specs
// import `cloudflare:test` and only resolve under the workers pool; vitest's
// default include would glob them into this run and fail at import. The
// Worker's suite runs from worker/vitest.config.mts - the two configs are
// mutually exclusive (node environment vs. defineWorkersConfig), which is why
// the Worker is a separate package rather than more files here.
export default defineConfig({
	test: {
		environment: "node",
		include: ["test/**/*.spec.ts"],
	},
});
