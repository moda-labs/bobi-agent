import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig({
	test: {
		poolOptions: {
			workers: {
				// Disable isolated storage - the DELETE path exercises Durable
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
						FLEET_OPERATOR_TOKEN: "test-operator-token",
						FLEET_LIVE_WINDOW_S: "60",
						FLEET_STALE_WINDOW_S: "120",
						// Short so the suite does not spend the 5s production
						// budget on every deliberately-unanswered command. The
						// default itself is asserted as a unit in mcp.spec.ts.
						MCP_COMMAND_WAIT_MS: "1500",
					},
				},
			},
		},
	},
});
