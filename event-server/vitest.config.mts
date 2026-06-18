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
			},
		},
	},
});
