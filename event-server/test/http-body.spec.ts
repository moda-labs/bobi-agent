import { describe, it, expect } from "vitest";
import { EventEmitter } from "node:events";
import type http from "node:http";

import { readBody, BodyTooLargeError, MAX_REQUEST_BODY_BYTES } from "../src/http-body";

// A minimal stand-in for http.IncomingMessage: readBody only uses the data/end/
// error events and destroy(), so a real socket would add nothing but a port.
function fakeReq() {
	const req = new EventEmitter() as EventEmitter & {
		destroy: () => void;
		destroyCalls: number;
	};
	req.destroyCalls = 0;
	req.destroy = () => {
		req.destroyCalls += 1;
	};
	return req;
}

describe("readBody", () => {
	it("reads a body under the cap", async () => {
		const req = fakeReq();
		const p = readBody(req as unknown as http.IncomingMessage, 1024);
		req.emit("data", Buffer.from("hello "));
		req.emit("data", Buffer.from("world"));
		req.emit("end");
		expect(await p).toBe("hello world");
		expect(req.destroyCalls).toBe(0);
	});

	// D049 — the cap must bound the READ. Before the fix, readBody concatenated
	// every chunk unconditionally and the size gate lived downstream in core,
	// where it could only run after the whole body was already in memory.
	it("rejects and destroys the request on the chunk that crosses the cap (D049)", async () => {
		const req = fakeReq();
		const p = readBody(req as unknown as http.IncomingMessage, 1024);
		const rejected = expect(p).rejects.toBeInstanceOf(BodyTooLargeError);

		const chunk = Buffer.alloc(512, 0x61);
		req.emit("data", chunk); // 512
		expect(req.destroyCalls).toBe(0);
		req.emit("data", chunk); // 1024 — exactly at the cap, still fine
		expect(req.destroyCalls).toBe(0);
		req.emit("data", chunk); // 1536 — over

		await rejected;
		// The transfer is stopped rather than drained, so the sender cannot keep
		// spending our memory after the limit is known.
		expect(req.destroyCalls).toBe(1);
	});

	it("does not buffer past the cap even when the sender keeps writing (D049)", async () => {
		const req = fakeReq();
		const p = readBody(req as unknown as http.IncomingMessage, 64);
		const rejected = expect(p).rejects.toThrow(/exceeds 64 bytes/);

		req.emit("data", Buffer.alloc(65));
		// A sender ignoring the destroy keeps pushing; nothing may accumulate and
		// the settled promise must not be re-settled by the later `end`.
		for (let i = 0; i < 100; i++) req.emit("data", Buffer.alloc(1024));
		req.emit("end");

		await rejected;
		expect(req.destroyCalls).toBe(1);
	});

	it("propagates a transport error", async () => {
		const req = fakeReq();
		const p = readBody(req as unknown as http.IncomingMessage, 1024);
		const rejected = expect(p).rejects.toThrow(/boom/);
		req.emit("error", new Error("boom"));
		await rejected;
	});

	it("defaults to a bounded cap", () => {
		expect(MAX_REQUEST_BODY_BYTES).toBeGreaterThan(0);
		expect(MAX_REQUEST_BODY_BYTES).toBeLessThanOrEqual(64 * 1024 * 1024);
	});
});
