// Request-body reading for the standalone Node event server.
//
// Lives beside local.ts rather than inside it because local.ts is an
// entrypoint: importing it binds a port and starts the eviction sweep, so
// nothing there can be unit-tested. Same reason slack-socket-local.ts is a
// sibling.

import type http from "node:http";

// D049 — the cap has to bound the READ, not the already-read buffer.
//
// Every route here reads the whole body into memory before any handler sees
// it, and the ingest route's own 256KB gate (INGEST_MAX_BODY_BYTES, checked in
// core against `rawBody.length`) can only fire once that read has completed —
// the memory is spent by the time it says 413. On a server bound non-loopback
// (BOBI_ES_BIND, the documented self-hosted deployment) the webhook route reads
// the body BEFORE signature verification, so an unauthenticated client can pick
// the peak. A handful of concurrent multi-gigabyte POSTs OOM-kills the process,
// taking every live agent WebSocket delivery with it.
//
// The default is generous on purpose: /channels/send inlines file uploads as
// base64 (content_b64), which inflates real bytes by 4/3, so a low ceiling
// would reject legitimate traffic. 8 MiB leaves room for ~6 MB of file while
// still bounding the worst case, and route-specific gates keep firing first
// with their own, more specific errors. Operators who genuinely send more can
// raise it.
const DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024;

function configuredMax(): number {
	const raw = parseInt(process.env.BOBI_ES_MAX_BODY_BYTES || "", 10);
	return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_MAX_BODY_BYTES;
}

export const MAX_REQUEST_BODY_BYTES = configuredMax();

/** Thrown by readBody when the request body exceeds the cap. Maps to 413. */
export class BodyTooLargeError extends Error {
	readonly limit: number;

	constructor(limit: number) {
		super(`request body exceeds ${limit} bytes`);
		this.name = "BodyTooLargeError";
		this.limit = limit;
	}
}

/**
 * Read a request body to a string, aborting as soon as it exceeds `limit`.
 *
 * Rejects with BodyTooLargeError and destroys the socket on the chunk that
 * crosses the cap, so the bytes past that point are never accumulated.
 */
export function readBody(
	req: http.IncomingMessage,
	limit: number = MAX_REQUEST_BODY_BYTES,
): Promise<string> {
	return new Promise((resolve, reject) => {
		const chunks: Buffer[] = [];
		let total = 0;
		let aborted = false;

		req.on("data", (chunk: Buffer) => {
			if (aborted) return;
			total += chunk.length;
			if (total > limit) {
				// Drop what we hold and stop the transfer; the promise is already
				// settled, so the `error` destroy() raises is a no-op below.
				aborted = true;
				chunks.length = 0;
				reject(new BodyTooLargeError(limit));
				req.destroy();
				return;
			}
			chunks.push(chunk);
		});
		req.on("end", () => {
			if (!aborted) resolve(Buffer.concat(chunks).toString());
		});
		req.on("error", (err) => {
			if (!aborted) reject(err);
		});
	});
}
