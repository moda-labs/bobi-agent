import { describe, it, expect } from "vitest";
import {
	chunkForChannel,
	truncateForChannel,
	type ChannelCapabilities,
} from "../src/channels";

// Small budgets keep the fixtures readable; the algorithm is budget-agnostic.
function caps(maxLength: number, lengthUnit: "chars" | "utf16" = "utf16"): ChannelCapabilities {
	return {
		edit: true,
		typing: true,
		streaming: "native",
		threads: true,
		files: true,
		maxLength,
		lengthUnit,
	};
}

describe("chunkForChannel", () => {
	it("returns within-budget text as a single byte-identical chunk", () => {
		const text = "hello **world**\n\n```py\nx = 1\n```";
		expect(chunkForChannel(text, caps(1000))).toEqual([text]);
	});

	it("splits at the last paragraph break inside the budget", () => {
		const text = "first paragraph.\n\nsecond paragraph.\n\nthird one here.";
		const chunks = chunkForChannel(text, caps(30));
		expect(chunks.length).toBeGreaterThan(1);
		expect(chunks[0]).toBe("first paragraph.");
		// No chunk overshoots and nothing is lost or marker-truncated.
		for (const c of chunks) expect(c.length).toBeLessThanOrEqual(30);
		expect(chunks.join("\n")).not.toContain("_(truncated)_");
		expect(chunks.join(" ")).toContain("third one here.");
	});

	it("falls back to a line break when no paragraph break fits", () => {
		const text = "line one is here\nline two is here\nline three here";
		const chunks = chunkForChannel(text, caps(40));
		expect(chunks.length).toBeGreaterThan(1);
		for (const c of chunks) {
			expect(c.length).toBeLessThanOrEqual(40);
			expect(c.startsWith("\n")).toBe(false);
		}
		expect(chunks.join("\n")).toBe(text);
	});

	it("hard-splits unbroken text and loses nothing", () => {
		const text = "x".repeat(95);
		const chunks = chunkForChannel(text, caps(40));
		for (const c of chunks) expect(c.length).toBeLessThanOrEqual(40);
		expect(chunks.join("")).toBe(text);
	});

	it("closes and reopens a code fence split across chunks", () => {
		const code = Array.from({ length: 12 }, (_, i) => `line_${i} = ${i}`).join("\n");
		const text = `intro\n\n\`\`\`python\n${code}\n\`\`\`\n\ndone`;
		const chunks = chunkForChannel(text, caps(80));
		expect(chunks.length).toBeGreaterThan(1);
		for (const c of chunks) {
			expect(c.length).toBeLessThanOrEqual(80);
			// Every chunk renders: fences are balanced within each chunk.
			const fenceLines = c.split("\n").filter((l) => /^\s*```/.test(l));
			expect(fenceLines.length % 2).toBe(0);
		}
		// A continuation chunk reopens with the original info string.
		const reopened = chunks.filter((c) => c.startsWith("```python\n"));
		expect(reopened.length).toBeGreaterThan(0);
		// All code lines survive across the chunks.
		const joined = chunks.join("\n");
		expect(joined).toContain("line_0 = 0");
		expect(joined).toContain("line_11 = 11");
		expect(joined).toContain("done");
	});

	it("never splits a surrogate pair on a hard cut (utf16 unit)", () => {
		const text = "\u{1F600}".repeat(50); // 100 UTF-16 units of emoji
		const chunks = chunkForChannel(text, caps(33));
		for (const c of chunks) {
			expect(c.length).toBeLessThanOrEqual(33);
			// A split pair leaves a lone surrogate; the chunk must stay
			// well-formed UTF-16 end to end.
			expect(c.isWellFormed()).toBe(true);
		}
		expect(chunks.join("")).toBe(text);
	});

	it("counts code points when lengthUnit is chars", () => {
		const text = "\u{1F600}".repeat(10); // 10 chars, 20 UTF-16 units
		// Budget of 10 chars: fits as one chunk under "chars"...
		expect(chunkForChannel(text, caps(10, "chars"))).toEqual([text]);
		// ...but needs splitting under "utf16".
		expect(chunkForChannel(text, caps(10, "utf16")).length).toBeGreaterThan(1);
	});

	it("caps pathological input at the chunk guard and truncates the tail", () => {
		const text = "y".repeat(10_000);
		const chunks = chunkForChannel(text, caps(100));
		expect(chunks.length).toBeLessThanOrEqual(8);
		expect(chunks[chunks.length - 1].endsWith("_(truncated)_")).toBe(true);
		for (const c of chunks) expect(c.length).toBeLessThanOrEqual(100);
	});
});

describe("truncateForChannel", () => {
	it("is unit-aware: astral text under the chars budget passes untouched", () => {
		const text = "\u{1F600}".repeat(10);
		expect(truncateForChannel(text, caps(10, "chars"))).toBe(text);
		expect(truncateForChannel(text, caps(10, "utf16")).endsWith("_(truncated)_")).toBe(true);
	});
});
