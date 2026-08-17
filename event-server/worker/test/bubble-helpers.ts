// ---------------------------------------------------------------------------
// Bubble-signed request helpers, shared by the suites that need a REAL
// deployment on the bus rather than a seeded KV record.
//
// Anything exercising the command path needs all three: a bubble to sign as, an
// admin subscription so the command has a live subscriber to deliver to, and a
// signed heartbeat so the server has captured the bubble and can address the
// deployment. Seeding KV directly is enough for a read, and not enough here.
// ---------------------------------------------------------------------------

import { SELF } from "cloudflare:test";
import { buildBubbleSignature } from "@moda-labs/bobi-events-core";

export interface Bubble {
	bubble_id: string;
	bubble_key: string;
}

/** Signed headers for one request, as the sidecar builds them. */
async function signedHeaders(
	bubble: Bubble,
	method: string,
	path: string,
	body: string,
): Promise<Record<string, string>> {
	const timestamp = String(Math.floor(Date.now() / 1000));
	const nonce = `n-${Math.random().toString(16).slice(2)}`;
	return {
		"content-type": "application/json",
		"x-moda-bubble": bubble.bubble_id,
		"x-moda-algo": "hmac-sha256",
		"x-moda-timestamp": timestamp,
		"x-moda-nonce": nonce,
		"x-moda-signature": await buildBubbleSignature(bubble.bubble_key, timestamp, nonce, method, path, body),
	};
}

export async function mintBubble(): Promise<Bubble> {
	const res = await SELF.fetch("https://example.com/deployments", {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify({ name: `mint-${Date.now()}-${Math.random().toString(16).slice(2)}`, subscriptions: ["_bootstrap"] }),
	});
	if (res.status !== 201) throw new Error(`mintBubble failed: ${res.status}`);
	return res.json();
}

export async function publishSigned(topic: string, bubble: Bubble, payload: unknown): Promise<Response> {
	const path = `/events/${topic}`;
	const body = JSON.stringify({ source: "fleet", payload });
	return SELF.fetch(`https://example.com${path}`, {
		method: "POST",
		headers: await signedHeaders(bubble, "POST", path, body),
		body,
	});
}

/**
 * Register a deployment (JOIN the existing bubble via a signed POST) subscribed
 * to `subscriptions` - mirrors what the sidecar's admin listener does so the
 * server has a live subscription for a command to deliver to.
 */
export async function registerSigned(
	name: string,
	subscriptions: string[],
	bubble: Bubble,
): Promise<Response> {
	const path = "/deployments";
	const body = JSON.stringify({ name, subscriptions });
	return SELF.fetch(`https://example.com${path}`, {
		method: "POST",
		headers: await signedHeaders(bubble, "POST", path, body),
		body,
	});
}
