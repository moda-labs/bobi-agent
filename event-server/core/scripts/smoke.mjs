// Scratch-consumer smoke for the publishable events-core tarball.
//
// Proves the artifact scripts/pack.mjs produces actually works for an
// external consumer with no workspace context: installs the tarball into a
// temp project, imports the root and every subpath export from plain Node
// (validating the compiled ESM specifiers), exercises a few pure functions,
// and typechecks a TS consumer against the shipped .d.ts files.
//
// Run via `npm run smoke -w core` from event-server/ (CI does).

import { createRequire } from "node:module";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { packCore, run } from "./pack.mjs";

const require = createRequire(import.meta.url);

const scratch = mkdtempSync(path.join(tmpdir(), "bobi-events-core-smoke-"));
try {
	const { tarball, version, exports: exportsMap } = packCore({ dest: scratch });
	console.log(`packed ${tarball}`);

	writeFileSync(
		path.join(scratch, "package.json"),
		JSON.stringify({ name: "events-core-smoke", private: true, type: "module" }, null, "\t"),
	);
	run("npm", ["install", "--prefer-offline", "--no-audit", "--no-fund", tarball], scratch);

	// Node runtime import of every export subpath, derived from the manifest
	// so a new entry is covered (or a broken one caught) automatically.
	const specifiers = Object.keys(exportsMap).map(
		(subpath) => `@moda-labs/bobi-events-core${subpath.slice(1)}`,
	);
	const imports = specifiers
		.map((s, i) => `import * as ns${i} from ${JSON.stringify(s)};`)
		.join("\n");
	const nsChecks = specifiers
		.map((s, i) => `assert.ok(Object.keys(ns${i}).length > 0, ${JSON.stringify(`empty module: ${s}`)});`)
		.join("\n");
	writeFileSync(path.join(scratch, "main.mjs"), `
		import assert from "node:assert/strict";
		${imports}
		${nsChecks}
		import { sha256Hex } from "@moda-labs/bobi-events-core";
		import { buildConversation, parseConversation } from "@moda-labs/bobi-events-core/conversation";

		assert.equal(
			await sha256Hex("bobi"),
			"7b38085de7defcec794220ebf215fe715e402bb938079b59ad1ae2481db46c09",
		);
		const conv = { source: "slack", scope: "T123", chatType: "channel", chatId: "C123", threadId: "171.2" };
		assert.deepEqual(parseConversation(buildConversation(conv)), conv);
	`);
	run("node", ["main.mjs"], scratch);
	console.log("node consumer ok");

	// TS consumer typecheck against the shipped declarations, using the same
	// moduleResolution the private worker repo uses. skipLibCheck stays off
	// so the shipped d.ts files themselves must be valid for a consumer.
	writeFileSync(path.join(scratch, "consumer.ts"), `
		import { type NormalizedEvent, parseGlobalTopic } from "@moda-labs/bobi-events-core";
		import { conversationKey } from "@moda-labs/bobi-events-core/circuit-breaker";

		export function smokeKey(event: NormalizedEvent): string | null {
			return conversationKey(event);
		}
		export const parsed: { service: string; resource: string } | null = parseGlobalTopic("slack:T123");
	`);
	writeFileSync(
		path.join(scratch, "tsconfig.json"),
		JSON.stringify({
			compilerOptions: {
				target: "es2022",
				lib: ["es2024", "dom"],
				module: "es2022",
				moduleResolution: "bundler",
				strict: true,
				noEmit: true,
			},
			include: ["consumer.ts"],
		}, null, "\t"),
	);
	run(process.execPath, [require.resolve("typescript/bin/tsc"), "-p", "tsconfig.json"], scratch);
	console.log("ts consumer ok");

	console.log(`events-core ${version} publish smoke passed`);
} finally {
	rmSync(scratch, { recursive: true, force: true });
}
