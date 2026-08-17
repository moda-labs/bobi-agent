import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig({
	test: {
		// Vitest's 5s default is a NODE default, and this suite is not Node: every
		// test drives a real workerd through miniflare, where one assertion can
		// serialize a dozen HTTP round-trips, a Durable Object hop, and several KV
		// reads. On a loaded CI runner those inflate an order of magnitude at once
		// - the 2026-08-13 run that reddened `main` reported this suite's
		// sleep-free tests at ~20x their local time - and legitimate runs land
		// either side of 5s. That run has both halves of the proof: mcp.spec.ts's
		// transcript test failed at 5538ms, and index.spec.ts's multi-bubble test
		// PASSED at 5056ms, on a one-off 15s override added the last time someone
		// hit this. One suite-wide budget, so the next test over the line is not a
		// third one-off (#1028).
		//
		// Nothing here asserts timing through this number, so widening it hides
		// nothing: every timing claim in the suite is asserted structurally
		// instead - by poll count, or by which fields the payload carries. This is
		// the ceiling for a HANG, and the settle helpers in mcp.spec.ts bound
		// themselves well inside it so a stuck wait names its own subject rather
		// than surfacing as a bare "Test timed out".
		testTimeout: 15_000,
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
