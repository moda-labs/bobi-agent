import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig({
	test: {
		poolOptions: {
			workers: {
				// Disable isolated storage — the DELETE path exercises Durable
				// Object storage, and the pool's transaction-based rollback
				// triggers "Isolated storage failed" on DO teardown (#305).
				// Tests are independent enough not to need per-test rollback.
				isolatedStorage: false,
				wrangler: { configPath: "./wrangler.jsonc" },
				miniflare: {
					bindings: {
						INTERNAL_DO_SECRET: "test-internal-secret",
						BOBI_RELEASE_VERSION: "test-version",
						BOBI_RELEASE_SHA: "test-sha",
					},
				},
			},
		},
	},
});
