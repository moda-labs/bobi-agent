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
//   3. npm pack the stage into the dest dir (default: event-server/core/)
//
// Usage: node scripts/pack.mjs

import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const coreDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repoRoot = path.dirname(path.dirname(coreDir));
const require = createRequire(import.meta.url);

export function run(cmd, args, cwd) {
	const res = spawnSync(cmd, args, { cwd, stdio: ["ignore", "inherit", "inherit"] });
	if (res.error) {
		throw new Error(`${cmd} ${args.join(" ")} failed to spawn`, { cause: res.error });
	}
	if (res.status !== 0) {
		throw new Error(`${cmd} ${args.join(" ")} failed with status ${res.status}`);
	}
}

// Derive the published exports map from the source one so the two shapes
// cannot drift: ./src/x.ts -> { types: ./dist/x.d.ts, default: ./dist/x.js }.
// Anything that is not a plain ./src/*.ts target has no derivable dist
// counterpart and must fail the pack rather than ship dangling paths.
function publishExports(srcExports) {
	const out = {};
	for (const [subpath, target] of Object.entries(srcExports)) {
		if (typeof target !== "string" || !/^\.\/src\/[\w./-]+\.ts$/.test(target)) {
			throw new Error(`unsupported exports target for "${subpath}": ${JSON.stringify(target)}`);
		}
		const stem = target.replace(/^\.\/src\//, "./dist/").replace(/\.ts$/, "");
		out[subpath] = { types: `${stem}.d.ts`, default: `${stem}.js` };
	}
	// The manifest itself stays reachable through the exports encapsulation
	// (resolvers, license scanners, bundler plugins).
	out["./package.json"] = "./package.json";
	return out;
}

export function packCore({ dest = coreDir } = {}) {
	dest = path.resolve(dest);
	rmSync(path.join(coreDir, "dist"), { recursive: true, force: true });
	run(process.execPath, [require.resolve("typescript/bin/tsc"), "-p", "tsconfig.json"], coreDir);

	const manifest = JSON.parse(readFileSync(path.join(coreDir, "package.json"), "utf8"));
	const stage = mkdtempSync(path.join(tmpdir(), "bobi-events-core-pack-"));
	try {
		cpSync(path.join(coreDir, "dist"), path.join(stage, "dist"), { recursive: true });
		cpSync(path.join(coreDir, "src"), path.join(stage, "src"), { recursive: true });
		cpSync(path.join(coreDir, "README.md"), path.join(stage, "README.md"));
		cpSync(path.join(repoRoot, "LICENSE"), path.join(stage, "LICENSE"));

		const { private: _private, devDependencies: _dev, scripts: _scripts, ...publish } = manifest;
		publish.exports = publishExports(manifest.exports);
		// Fallbacks for exports-unaware resolvers (node10 moduleResolution,
		// tools that only read main/types).
		publish.main = "./dist/core.js";
		publish.types = "./dist/core.d.ts";
		writeFileSync(path.join(stage, "package.json"), JSON.stringify(publish, null, "\t") + "\n");

		run("npm", ["pack", "--pack-destination", dest], stage);
	} finally {
		rmSync(stage, { recursive: true, force: true });
	}

	// npm mangles scoped names: @scope/name -> scope-name.
	const basename = manifest.name.replace(/^@/, "").replace("/", "-");
	return {
		tarball: path.join(dest, `${basename}-${manifest.version}.tgz`),
		version: manifest.version,
		exports: manifest.exports,
	};
}

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
	console.log(packCore().tarball);
}
