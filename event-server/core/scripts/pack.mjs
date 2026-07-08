// Build the publishable tarball for @moda-labs/bobi-events-core.
//
// The workspace manifest stays `private: true` with exports pointing at
// src/*.ts: everything in-repo (root tsc, vitest, esbuild local bundle,
// wrangler, and the lazy runtime build from the installed wheel) consumes
// the TypeScript sources directly. The published package instead ships
// compiled ESM + .d.ts, so external consumers (the private worker adapter)
// pin a plain npm dependency with no TS build coupling.
//
// This script is the single place that produces that shape:
//   1. tsc emit into dist/ (js + d.ts + source/declaration maps)
//   2. stage a temp dir with dist/, src/ (for the maps), README, LICENSE,
//      and a transformed package.json whose exports point at dist/
//   3. npm pack the stage into --dest (default: event-server/core/)
//
// Usage: node scripts/pack.mjs [--dest <dir>]

import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const coreDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repoRoot = path.dirname(path.dirname(coreDir));

function run(cmd, args, cwd) {
	const res = spawnSync(cmd, args, { cwd, stdio: ["ignore", "inherit", "inherit"] });
	if (res.status !== 0) {
		throw new Error(`${cmd} ${args.join(" ")} failed with status ${res.status}`);
	}
}

// Derive the published exports map from the source one so the two shapes
// cannot drift: ./src/x.ts -> { types: ./dist/x.d.ts, default: ./dist/x.js }.
function publishExports(srcExports) {
	const out = {};
	for (const [subpath, target] of Object.entries(srcExports)) {
		const stem = target.replace(/^\.\/src\//, "./dist/").replace(/\.ts$/, "");
		out[subpath] = { types: `${stem}.d.ts`, default: `${stem}.js` };
	}
	return out;
}

export function packCore({ dest = coreDir } = {}) {
	rmSync(path.join(coreDir, "dist"), { recursive: true, force: true });
	run("npm", ["run", "build"], coreDir);

	const manifest = JSON.parse(readFileSync(path.join(coreDir, "package.json"), "utf8"));
	const stage = mkdtempSync(path.join(tmpdir(), "bobi-events-core-pack-"));
	try {
		cpSync(path.join(coreDir, "dist"), path.join(stage, "dist"), { recursive: true });
		cpSync(path.join(coreDir, "src"), path.join(stage, "src"), { recursive: true });
		cpSync(path.join(coreDir, "README.md"), path.join(stage, "README.md"));
		cpSync(path.join(repoRoot, "LICENSE"), path.join(stage, "LICENSE"));

		const { private: _private, devDependencies: _dev, scripts: _scripts, ...publish } = manifest;
		publish.exports = publishExports(manifest.exports);
		writeFileSync(path.join(stage, "package.json"), JSON.stringify(publish, null, "\t") + "\n");

		run("npm", ["pack", "--pack-destination", dest], stage);
	} finally {
		rmSync(stage, { recursive: true, force: true });
	}

	const tarball = path.resolve(
		dest,
		`moda-labs-bobi-events-core-${manifest.version}.tgz`,
	);
	return { tarball, version: manifest.version };
}

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
	const destFlag = process.argv.indexOf("--dest");
	const dest = destFlag === -1 ? coreDir : path.resolve(process.argv[destFlag + 1]);
	const { tarball } = packCore({ dest });
	console.log(tarball);
}
