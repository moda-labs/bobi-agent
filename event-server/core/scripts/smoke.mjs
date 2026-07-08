// Scratch-consumer smoke for the publishable events-core tarball.
//
// Proves the artifact scripts/pack.mjs produces actually works for an
// external consumer with no workspace context: installs the tarball into a
// temp project, imports the root and every subpath export from plain Node
// (validating the compiled ESM specifiers), exercises a few pure functions,
// and typechecks a TS consumer against the shipped .d.ts files.
//
// Run via `npm run smoke -w core` from event-server/ (CI does).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { packCore } from "./pack.mjs";

const require = createRequire(import.meta.url);

function run(cmd, args, cwd) {
	const res = spawnSync(cmd, args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
	if (res.status !== 0) {
		throw new Error(
			`${cmd} ${args.join(" ")} failed with status ${res.status}\n${res.stdout}\n${res.stderr}`,
		);
	}
	return res.stdout.toString();
}

const scratch = mkdtempSync(path.join(tmpdir(), "bobi-events-core-smoke-"));
try {
	const { tarball, version } = packCore({ dest: scratch });
	console.log(`packed ${tarball}`);

	writeFileSync(
		path.join(scratch, "package.json"),
		JSON.stringify({ name: "events-core-smoke", private: true, type: "module" }, null, "\t"),
	);
	run("npm", ["install", "--no-audit", "--no-fund", tarball], scratch);

	// Node runtime import of the root and every subpath export.
	writeFileSync(path.join(scratch, "main.mjs"), `
		import assert from "node:assert/strict";
		import { parseGlobalTopic, sha256Hex, constantTimeEqual, namespaceSubKey } from "@moda-labs/bobi-events-core";
		import { setSlackApiUrl, setWhatsAppApiUrl } from "@moda-labs/bobi-events-core/channels";
		import { buildConversation, parseConversation } from "@moda-labs/bobi-events-core/conversation";
		import { BREAKER_THRESHOLD, conversationKey } from "@moda-labs/bobi-events-core/circuit-breaker";
		import { bridgeSlackWebhook } from "@moda-labs/bobi-events-core/adapters/chat-sdk-slack";

		assert.equal(typeof parseGlobalTopic, "function");
		assert.equal(typeof setSlackApiUrl, "function");
		assert.equal(typeof setWhatsAppApiUrl, "function");
		assert.equal(typeof bridgeSlackWebhook, "function");
		assert.equal(typeof conversationKey, "function");
		assert.ok(BREAKER_THRESHOLD > 0);
		assert.equal(constantTimeEqual("abc", "abc"), true);
		assert.equal(constantTimeEqual("abc", "abd"), false);
		assert.equal(typeof namespaceSubKey, "function");
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
	// moduleResolution the private worker repo uses.
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
				module: "es2022",
				moduleResolution: "bundler",
				strict: true,
				noEmit: true,
				skipLibCheck: true,
			},
			include: ["consumer.ts"],
		}, null, "\t"),
	);
	const tscBin = require.resolve("typescript/bin/tsc");
	run(process.execPath, [tscBin, "-p", "tsconfig.json"], scratch);
	console.log("ts consumer ok");

	console.log(`events-core ${version} publish smoke passed`);
} finally {
	rmSync(scratch, { recursive: true, force: true });
}
