import { SELF, env } from "cloudflare:test";
import { describe, it, expect, afterEach, vi } from "vitest";
import worker from "../src/index";
import { buildBubbleSignature, parseGlobalTopic } from "../src/core";
import {
	INTERNAL_HEADER,
	INTERNAL_WS_PROTOCOL_PREFIX,
	internalWebSocketProtocol,
	internalWebSocketRequest,
} from "../src/internal-auth";

afterEach(() => vi.unstubAllGlobals());

function stubSlackFetch(teamId: string, botId = "B1", appId = "A1", chat: "ok" | "fail" = "ok") {
	vi.stubGlobal("fetch", vi.fn(async (url: string | URL) => {
		const u = String(url);
		if (u.includes("/auth.test")) {
			return Response.json({ ok: true, team_id: teamId, bot_id: botId });
		}
		if (u.includes("/bots.info")) {
			return Response.json({ ok: true, bot: { app_id: appId } });
		}
		if (u.includes("/chat.postMessage")) {
			if (chat === "fail") throw new Error("slack unavailable");
			return Response.json({ ok: true, ts: "1.2" });
		}
		return Response.json({ ok: true });
	}));
}

// Seed a resource grant directly in KV (bypassing upstream verification) so a
// deployment can legitimately subscribe to a global topic under #488. The grant
// is keyed by the topic's parsed {service, resource} — for slack, parseGlobalTopic
// reduces `slack:team:channel` to the team id, matching how the gate looks it up.
async function seedGrant(topic: string, bubbleId: string): Promise<void> {
	const parsed = parseGlobalTopic(topic);
	if (!parsed) throw new Error(`not a global topic: ${topic}`);
	await env.EVENTS.put(
		`resource_grant:${parsed.service}:${parsed.resource}:${bubbleId}`,
		JSON.stringify({
			id: `grant-${parsed.service}-${parsed.resource}-${bubbleId}`,
			account_id: null,
			bubble_id: bubbleId,
			service: parsed.service,
			resource: parsed.resource,
			granted_by: "upstream_token_verification",
			created_at: "2026-01-01T00:00:00Z",
			expires_at: null,
		}),
	);
}

// Mint a bubble, grant every requested global topic, then JOIN-register a
// deployment subscribing to them — the grant-aware analogue of mintBubble for
// the #488 enforcement world.
let _depCounter = 0;
async function deploymentWithGrants(subscriptions: string[]): Promise<MintedBubble> {
	const b = await mintBubble(["_bootstrap"]);
	for (const s of subscriptions) {
		if (parseGlobalTopic(s)) await seedGrant(s, b.bubble_id);
	}
	const body = JSON.stringify({ name: `dep-${Date.now()}-${_depCounter++}`, subscriptions });
	const headers = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/deployments", body);
	const res = await SELF.fetch("https://example.com/deployments", {
		method: "POST",
		headers: { "content-type": "application/json", ...headers },
		body,
	});
	if (res.status !== 201) throw new Error(`deploymentWithGrants failed: ${res.status}`);
	const reg = (await res.json()) as { deployment_id: string; api_key: string };
	return { ...reg, bubble_id: b.bubble_id, bubble_key: b.bubble_key };
}

// ---------------------------------------------------------------------------
// Bubble-signing helpers (mirrors core.spec.ts helpers but for HTTP-level
// tests through SELF.fetch — the canonical string covers the exact wire bytes)
// ---------------------------------------------------------------------------

interface MintedBubble {
	deployment_id: string;
	api_key: string;
	bubble_id: string;
	bubble_key: string;
}

/** Register an unsigned deployment to mint a fresh bubble. */
async function mintBubble(subscriptions: string[] = ["test:topic"]): Promise<MintedBubble> {
	const res = await SELF.fetch("https://example.com/deployments", {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify({ name: `mint-${Date.now()}`, subscriptions }),
	});
	if (res.status !== 201) throw new Error(`mintBubble failed: ${res.status}`);
	return res.json() as Promise<MintedBubble>;
}

/** Build bubble-signing headers for a request. */
async function bubbleHeaders(
	bubbleId: string,
	bubbleKey: string,
	method: string,
	path: string,
	body: string,
): Promise<Record<string, string>> {
	const timestamp = String(Math.floor(Date.now() / 1000));
	const nonce = `n-${Date.now()}`;
	const signature = await buildBubbleSignature(bubbleKey, timestamp, nonce, method, path, body);
	return {
		"x-moda-bubble": bubbleId,
		"x-moda-algo": "hmac-sha256",
		"x-moda-timestamp": timestamp,
		"x-moda-nonce": nonce,
		"x-moda-signature": signature,
	};
}

describe("event-server", () => {
	it("health check returns ok", async () => {
		const response = await SELF.fetch("https://example.com/health");
		expect(response.status).toBe(200);
		const body = await response.json() as { status: string };
		expect(body.status).toBe("ok");
	});

	it("returns 404 for unknown routes", async () => {
		const response = await SELF.fetch("https://example.com/nope");
		expect(response.status).toBe(404);
	});

	it("rejects invalid JSON on github webhook", async () => {
		const response = await SELF.fetch("https://example.com/webhooks/github", {
			method: "POST",
			body: "not json",
		});
		expect(response.status).toBe(400);
	});

	it("rejects github webhook without repository", async () => {
		const response = await SELF.fetch("https://example.com/webhooks/github", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ action: "opened" }),
		});
		expect(response.status).toBe(400);
	});

	it("registers a deployment", async () => {
		const response = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				name: "test-deployment",
				subscriptions: ["moda-labs/modastack"],
			}),
		});
		expect(response.status).toBe(201);
		const body = await response.json() as { deployment_id: string; api_key: string };
		expect(body.deployment_id).toBeTruthy();
		expect(body.api_key).toMatch(/^moda_/);
	});

	it("rejects deployment registration without required fields", async () => {
		const response = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ name: "test" }),
		});
		expect(response.status).toBe(400);
	});

	it("rejects subscribe without auth", async () => {
		const response = await SELF.fetch(
			"https://example.com/deployments/some-id/subscribe",
		);
		expect(response.status).toBe(401);
	});

	it("rejects subscribe with invalid api key", async () => {
		const response = await SELF.fetch(
			"https://example.com/deployments/some-id/subscribe",
			{ headers: { authorization: "Bearer invalid_key" } },
		);
		expect(response.status).toBe(403);
	});

	it("rejects unsigned generic topic event", async () => {
		const response = await SELF.fetch("https://example.com/events/deploy.complete", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				source: "ci",
				payload: { status: "success", sha: "abc123" },
			}),
		});
		expect(response.status).toBe(403);
	});

	it("accepts signed generic topic event", async () => {
		const bubble = await mintBubble(["deploy.complete"]);
		const payload = JSON.stringify({
			source: "ci",
			payload: { status: "success", sha: "abc123" },
		});
		const path = "/events/deploy.complete";
		const headers = await bubbleHeaders(bubble.bubble_id, bubble.bubble_key, "POST", path, payload);
		const response = await SELF.fetch(`https://example.com${path}`, {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body: payload,
		});
		expect(response.status).toBe(200);
		const body = await response.json() as { delivered_to: number };
		expect(typeof body.delivered_to).toBe("number");
	});

	it("rejects generic topic with invalid JSON", async () => {
		const response = await SELF.fetch("https://example.com/events/test", {
			method: "POST",
			body: "not json",
		});
		expect(response.status).toBe(400);
	});

	it("rejects slack workspace registration without required fields", async () => {
		const response = await SELF.fetch("https://example.com/slack/workspaces", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ workspace_id: "T123" }),
		});
		expect(response.status).toBe(400);
	});

	it("rejects slack send without channel or text (signed, reaches 400)", async () => {
		const bubble = await mintBubble();
		const body = JSON.stringify({ text: "hello" });
		const headers = await bubbleHeaders(
			bubble.bubble_id, bubble.bubble_key, "POST", "/slack/send", body,
		);
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body,
		});
		expect(response.status).toBe(400);
	});

	// #487: outbound send must be authenticated.
	it("rejects unsigned slack send with 403", async () => {
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ workspace: "T1", channel: "C1", text: "hello" }),
		});
		expect(response.status).toBe(403);
	});

	it("rejects slack send with a bad signature (403)", async () => {
		const bubble = await mintBubble();
		const body = JSON.stringify({ workspace: "T1", channel: "C1", text: "hi" });
		const headers = await bubbleHeaders(
			bubble.bubble_id, bubble.bubble_key, "POST", "/slack/send", body,
		);
		headers["x-moda-signature"] = "deadbeef";
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body,
		});
		expect(response.status).toBe(403);
	});
});


// ---------------------------------------------------------------------------
// Cloudflare deregistration verification (#279)
// ---------------------------------------------------------------------------

describe("cloudflare deployment deregistration", () => {
	it("DELETE removes deployment from KV and cleans subscription-index entries", async () => {
		const regResp = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				name: "ephemeral-reply",
				subscriptions: ["reply/test-uuid"],
			}),
		});
		expect(regResp.status).toBe(201);
		const { deployment_id, api_key } = (await regResp.json()) as {
			deployment_id: string;
			api_key: string;
		};
		expect(deployment_id).toBeTruthy();

		const delResp = await SELF.fetch(
			`https://example.com/deployments/${deployment_id}`,
			{
				method: "DELETE",
				headers: { authorization: `Bearer ${api_key}` },
			},
		);
		expect(delResp.status).toBe(200);
		const delBody = (await delResp.json()) as { ok: boolean };
		expect(delBody.ok).toBe(true);

		const regResp2 = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				name: "ephemeral-reply-2",
				subscriptions: ["reply/test-uuid"],
			}),
		});
		expect(regResp2.status).toBe(201);
	});

	it("DELETE rejects wrong api key (403)", async () => {
		const regResp = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				name: "guarded-deploy",
				subscriptions: ["reply/guarded"],
			}),
		});
		const { deployment_id } = (await regResp.json()) as { deployment_id: string };

		const delResp = await SELF.fetch(
			`https://example.com/deployments/${deployment_id}`,
			{
				method: "DELETE",
				headers: { authorization: "Bearer wrong_key" },
			},
		);
		expect(delResp.status).toBe(403);
	});

	it("DELETE rejects unknown deployment id (403)", async () => {
		const delResp = await SELF.fetch(
			"https://example.com/deployments/nonexistent-id",
			{
				method: "DELETE",
				headers: { authorization: "Bearer any_key" },
			},
		);
		expect(delResp.status).toBe(403);
	});

	it("DELETE rejects missing auth header (403)", async () => {
		const delResp = await SELF.fetch(
			"https://example.com/deployments/some-id",
			{ method: "DELETE" },
		);
		expect(delResp.status).toBe(403);
	});
});

describe("slack send error handling", () => {
	it("returns 502 when slack API fetch fails", async () => {
		const bubble = await mintBubble();
		stubSlackFetch("T_FAIL", "B_FAIL", "A_FAIL", "fail");
		// Register a workspace SIGNED so the bubble-scoped record exists.
		const regBody = JSON.stringify({ workspace_id: "T_FAIL", bot_token: "xoxb-fake-token" });
		const regHeaders = await bubbleHeaders(
			bubble.bubble_id, bubble.bubble_key, "POST", "/slack/workspaces", regBody,
		);
		const regResponse = await SELF.fetch("https://example.com/slack/workspaces", {
			method: "POST",
			headers: { "content-type": "application/json", ...regHeaders },
			body: regBody,
		});
		expect(regResponse.status).toBe(200);

		// Now send (signed) — the fetch to slack.com will fail in the test sandbox
		const sendBody = JSON.stringify({ workspace: "T_FAIL", channel: "C123", text: "hello" });
		const sendHeaders = await bubbleHeaders(
			bubble.bubble_id, bubble.bubble_key, "POST", "/slack/send", sendBody,
		);
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json", ...sendHeaders },
			body: sendBody,
		});

		// Should get 502, not 500 — the try/catch maps fetch failures to 502
		expect(response.status).toBe(502);
		const body = await response.json() as { ok: boolean; error: string };
		expect(body.ok).toBe(false);
		expect(body.error).toBeTruthy();
	});

	// #487 end-to-end isolation through the Worker route: a workspace registered
	// by bubble B cannot be used by bubble A.
	it("does not let bubble A send through bubble B's workspace (400)", async () => {
		const bubbleB = await mintBubble();
		const bubbleA = await mintBubble();
		stubSlackFetch("T_ISO", "B_ISO", "A_ISO");

		// B registers T_ISO (signed → scoped to B).
		const regBody = JSON.stringify({ workspace_id: "T_ISO", bot_token: "xoxb-B-token" });
		const regHeaders = await bubbleHeaders(
			bubbleB.bubble_id, bubbleB.bubble_key, "POST", "/slack/workspaces", regBody,
		);
		const reg = await SELF.fetch("https://example.com/slack/workspaces", {
			method: "POST",
			headers: { "content-type": "application/json", ...regHeaders },
			body: regBody,
		});
		expect(reg.status).toBe(200);

		// A (valid signature, different bubble) tries to send through T_ISO → 400.
		const sendBody = JSON.stringify({ workspace: "T_ISO", channel: "C1", text: "hi" });
		const sendHeaders = await bubbleHeaders(
			bubbleA.bubble_id, bubbleA.bubble_key, "POST", "/slack/send", sendBody,
		);
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json", ...sendHeaders },
			body: sendBody,
		});
		expect(response.status).toBe(400);
		const body = await response.json() as { error: string };
		expect(body.error).toContain("bot token");
	});
});

describe("github webhook signature verification", () => {
	const secret = "test-webhook-secret";

	async function sign(body: string): Promise<string> {
		const key = await crypto.subtle.importKey(
			"raw",
			new TextEncoder().encode(secret),
			{ name: "HMAC", hash: "SHA-256" },
			false,
			["sign"],
		);
		const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
		const hex = Array.from(new Uint8Array(sig))
			.map((b) => b.toString(16).padStart(2, "0"))
			.join("");
		return `sha256=${hex}`;
	}

	it("rejects github webhook with invalid signature when WEBHOOK_SECRET is set", async () => {
		const payload = JSON.stringify({ action: "opened", repository: { full_name: "org/repo" } });
		const envWithSecret = { ...env, WEBHOOK_SECRET: secret };
		const response = await worker.fetch(
			new Request("https://example.com/webhooks/github", {
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-github-event": "issues",
					"x-hub-signature-256": "sha256=invalid",
				},
				body: payload,
			}),
			envWithSecret,
			{} as ExecutionContext,
		);
		expect(response.status).toBe(401);
	});

	it("accepts github webhook with valid signature when WEBHOOK_SECRET is set", async () => {
		const payload = JSON.stringify({ action: "opened", repository: { full_name: "org/repo" } });
		const signature = await sign(payload);
		const envWithSecret = { ...env, WEBHOOK_SECRET: secret };
		const response = await worker.fetch(
			new Request("https://example.com/webhooks/github", {
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-github-event": "issues",
					"x-hub-signature-256": signature,
				},
				body: payload,
			}),
			envWithSecret,
			{} as ExecutionContext,
		);
		expect(response.status).toBe(200);
	});

	it("accepts github webhook without signature when WEBHOOK_SECRET is not set", async () => {
		const response = await SELF.fetch("https://example.com/webhooks/github", {
			method: "POST",
			headers: {
				"content-type": "application/json",
				"x-github-event": "issues",
			},
			body: JSON.stringify({ action: "opened", repository: { full_name: "org/repo" } }),
		});
		expect(response.status).toBe(200);
	});
});

describe("cloudflare deployment deregistration", () => {
	it("DELETE removes deployment from KV and returns 200", async () => {
		// Register a deployment to get credentials (grant-backed global topic).
		const { deployment_id, api_key } = await deploymentWithGrants(["github:org/repo"]);

		// DELETE the deployment
		const delRes = await SELF.fetch(`https://example.com/deployments/${deployment_id}`, {
			method: "DELETE",
			headers: { authorization: `Bearer ${api_key}` },
		});
		expect(delRes.status).toBe(200);
		const body = await delRes.json() as { ok: boolean };
		expect(body.ok).toBe(true);
	});

	it("DELETE rejects unauthorized request", async () => {
		const response = await SELF.fetch("https://example.com/deployments/some-id", {
			method: "DELETE",
			headers: { authorization: "Bearer bad_key" },
		});
		expect(response.status).toBe(403);
	});

	it("DELETE rejects request without auth header", async () => {
		const response = await SELF.fetch("https://example.com/deployments/some-id", {
			method: "DELETE",
		});
		expect(response.status).toBe(403);
	});
});

// ---------------------------------------------------------------------------
// #341 — per-channel / per-repo delivery isolation (the routing layer of the
// "two live instances, disjoint channels/repos, no cross-delivery" acceptance).
// deliver() returns a COUNT of matched deployments; with unique team/channel/
// repo ids per test, an exact count pins down WHICH subscriber matched —
// "delivered to nobody" for an unscoped channel/repo is the cross-talk guard.
// The live two-instance events.jsonl check remains the final sign-off; this
// proves the Worker side end to end (register → ingest → match → count).
// ---------------------------------------------------------------------------
describe("slack url_verification handshake", () => {
	async function verify(headers: Record<string, string>, path = "/webhooks/slack"): Promise<Response> {
		return SELF.fetch(`https://example.com${path}`, {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body: JSON.stringify({ type: "url_verification", challenge: "abc123" }),
		});
	}

	it("echoes the challenge on the first attempt", async () => {
		const res = await verify({});
		expect(res.status).toBe(200);
		expect(await res.json()).toEqual({ challenge: "abc123" });
	});

	it("echoes the challenge with a trailing slash", async () => {
		const res = await verify({}, "/webhooks/slack/");
		expect(res.status).toBe(200);
		expect(await res.json()).toEqual({ challenge: "abc123" });
	});

	// Regression: the x-slack-retry-num short-circuit (event dedup) ran before
	// the url_verification handler, so a RETRIED handshake got {ok:true} with no
	// challenge and could never verify — leaving the request URL stuck unless a
	// human triggered a fresh (non-retry) attempt from the dashboard.
	it("still echoes the challenge when the handshake is retried", async () => {
		const res = await verify({ "x-slack-retry-num": "1" });
		expect(res.status).toBe(200);
		expect(await res.json()).toEqual({ challenge: "abc123" });
	});
});

describe("#341 targeted routing — no cross-delivery", () => {
	let uniq = 0;
	const id = (p: string) => `${p}_${Date.now()}_${uniq++}`;

	async function slackMessage(teamId: string, channel: string): Promise<number> {
		const res = await SELF.fetch("https://example.com/webhooks/slack", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				type: "event_callback",
				team_id: teamId,
				event: {
					type: "app_mention", channel, user: "U1",
					text: "hi", ts: `170000${uniq++}.000100`,
				},
			}),
		});
		expect(res.status).toBe(200);
		return (await res.json() as { delivered_to: number }).delivered_to;
	}

	async function githubIssue(repo: string): Promise<number> {
		const res = await SELF.fetch("https://example.com/webhooks/github", {
			method: "POST",
			headers: {
				"content-type": "application/json",
				"x-github-event": "issues",
				"x-github-delivery": id("d"),
			},
			body: JSON.stringify({
				action: "opened",
				repository: { full_name: repo },
				sender: { login: "u" },
				issue: {
					number: 1, title: "t", state: "open",
					html_url: `https://github.com/${repo}/issues/1`,
				},
			}),
		});
		expect(res.status).toBe(200);
		return (await res.json() as { delivered_to: number }).delivered_to;
	}

	it("slack: a channel message reaches only that channel's subscriber", async () => {
		const team = id("T");
		const eng = id("C_ENG");
		const sup = id("C_SUP");
		await deploymentWithGrants([`slack:${team}:${eng}`]);   // team A scoped to eng
		await deploymentWithGrants([`slack:${team}:${sup}`]);   // team B scoped to support

		expect(await slackMessage(team, eng)).toBe(1);   // only team A
		expect(await slackMessage(team, sup)).toBe(1);   // only team B
		// a channel nobody scoped to → delivered to nobody (no broadcast)
		expect(await slackMessage(team, id("C_OTHER"))).toBe(0);
	});

	it("slack: a whole-workspace subscriber is the explicit broadcast opt-in", async () => {
		const team = id("T");
		await deploymentWithGrants([`slack:${team}`]);  // bare workspace key = every channel
		expect(await slackMessage(team, id("C_ANY"))).toBe(1);
		expect(await slackMessage(team, id("C_ELSE"))).toBe(1);
	});

	it("github: an event reaches only that repo's subscriber", async () => {
		const org = id("org");
		await deploymentWithGrants([`github:${org}/repo-a`]);
		await deploymentWithGrants([`github:${org}/repo-b`]);

		expect(await githubIssue(`${org}/repo-a`)).toBe(1);   // only repo-a sub
		expect(await githubIssue(`${org}/repo-b`)).toBe(1);   // only repo-b sub
		// a repo nobody scoped to → delivered to nobody
		expect(await githubIssue(`${org}/repo-c`)).toBe(0);
	});
});

describe("#489 internal DeploymentSession auth", () => {
	const directStub = (deploymentId: string) => {
		const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
		return env.DEPLOYMENT_SESSION.get(doId);
	};

	it("rejects direct DO /init without the internal header", async () => {
		const response = await directStub("direct-missing-init").fetch(
			new Request("https://internal/init", {
				method: "POST",
				body: JSON.stringify({ deployment_id: "direct-missing-init", subscriptions: [] }),
			}),
		);

		expect(response.status).toBe(403);
		expect(await response.text()).toBe("");
	});

	it("rejects direct DO /event with the wrong internal header before parsing JSON", async () => {
		const response = await directStub("direct-bad-event").fetch(
			new Request("https://internal/event", {
				method: "POST",
				headers: { [INTERNAL_HEADER]: "wrong-secret" },
				body: "{not-json",
			}),
		);

		expect(response.status).toBe(403);
		expect(await response.text()).toBe("");
	});

	it("rejects direct DO websocket upgrades without the internal header", async () => {
		const response = await directStub("direct-missing-ws").fetch(
			new Request("https://internal/deployments/direct-missing-ws/subscribe", {
				headers: { Upgrade: "websocket" },
			}),
		);

		expect(response.status).toBe(403);
	});

	it("accepts direct DO /init and /event with the internal header", async () => {
		const stub = directStub("direct-good");
		const init = await stub.fetch(
			new Request("https://internal/init", {
				method: "POST",
				headers: { [INTERNAL_HEADER]: env.INTERNAL_DO_SECRET },
				body: JSON.stringify({ deployment_id: "direct-good", subscriptions: ["test:topic"] }),
			}),
		);
		expect(init.status).toBe(200);

		const event = await stub.fetch(
			new Request("https://internal/event", {
				method: "POST",
				headers: { [INTERNAL_HEADER]: env.INTERNAL_DO_SECRET },
				body: JSON.stringify({
					source: "test",
					topic: "test:topic",
					payload: { ok: true },
				}),
			}),
		);
		expect(event.status).toBe(200);
	});

	it("accepts direct DO websocket upgrades with the internal subprotocol token", async () => {
		const deploymentId = "direct-good-ws-protocol";
		const stub = directStub(deploymentId);
		const init = await stub.fetch(
			new Request("https://internal/init", {
				method: "POST",
				headers: { [INTERNAL_HEADER]: env.INTERNAL_DO_SECRET },
				body: JSON.stringify({ deployment_id: deploymentId, subscriptions: [] }),
			}),
		);
		expect(init.status).toBe(200);

		const response = await stub.fetch(
			new Request(`https://internal/deployments/${deploymentId}/subscribe`, {
				headers: {
					Upgrade: "websocket",
					"Sec-WebSocket-Protocol": internalWebSocketProtocol(env.INTERNAL_DO_SECRET),
				},
			}),
		);

		expect(response.status).toBe(101);
		expect(response.webSocket).toBeTruthy();
		response.webSocket?.accept();
		response.webSocket?.close();
	});

	it("worker-mediated /init and delivery still reach the DO", async () => {
		const bubble = await mintBubble(["internal-auth:topic"]);
		const payload = JSON.stringify({ source: "test", payload: { ok: true } });
		const path = "/events/internal-auth:topic";
		const headers = await bubbleHeaders(bubble.bubble_id, bubble.bubble_key, "POST", path, payload);

		const response = await SELF.fetch(`https://example.com${path}`, {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body: payload,
		});

		expect(response.status).toBe(200);
		expect(await response.json()).toMatchObject({ delivered_to: 1 });
	});

	it("all deliver branches add internal auth, including loop-detected and drained events", async () => {
		const bubble = await deploymentWithGrants(["slack:T_AUTH:C_AUTH"]);
		const slackEvent = (text: string, ts: string, botAuthored = false) => SELF.fetch("https://example.com/webhooks/slack", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				type: "event_callback",
				team_id: "T_AUTH",
				event: {
					type: "app_mention",
					channel: "C_AUTH",
					user: "U_AUTH",
					text,
					ts,
					thread_ts: "17000000.000000",
					...(botAuthored ? { bot_id: "B_AUTH" } : {}),
				},
			}),
		});

		for (let i = 0; i < 7; i++) {
			const response = await slackEvent("repeat", `1700000${i}.000100`, true);
			expect(response.status).toBe(200);
		}

		const unpause = await slackEvent("new human input", "17000008.000100");
		expect(unpause.status).toBe(200);

		const storedEvents = await Promise.all(
			Array.from({ length: 10 }, async (_, i) => {
				const data = await env.EVENTS.get(`events:${bubble.deployment_id}:${i + 1}`);
				return data ? JSON.parse(data) as { type: string } : null;
			}),
		);
		const storedTypes = storedEvents
			.filter((event): event is { type: string } => event !== null)
			.map((event) => event.type);

		expect(storedTypes).toContain("system.loop_detected");
		expect(storedTypes.filter((type) => type === "slack.mention")).toHaveLength(8);
	});

	it("websocket subscribe builds a fresh internal request without client auth headers", async () => {
		const request = internalWebSocketRequest(
			{ INTERNAL_DO_SECRET: "secret-value" },
			"https://example.com/deployments/dep-1/subscribe?last_seen=2",
		);

		expect(request.url).toBe("https://example.com/deployments/dep-1/subscribe?last_seen=2");
		expect(request.headers.get("Upgrade")).toBe("websocket");
		const protocol = request.headers.get("Sec-WebSocket-Protocol") || "";
		expect(protocol.startsWith(INTERNAL_WS_PROTOCOL_PREFIX)).toBe(true);
		expect(protocol).not.toContain("secret-value");
		expect(request.headers.get(INTERNAL_HEADER)).toBeNull();
		expect(request.headers.get("Authorization")).toBeNull();
		expect(request.headers.get("Cookie")).toBeNull();
		expect(Array.from(request.headers.keys()).sort()).toEqual([
			"sec-websocket-protocol",
			"upgrade",
		].sort());
	});

	it("worker-mediated websocket subscribe succeeds when the client sends ambient auth", async () => {
		const bubble = await mintBubble(["ws:auth"]);
		const response = await SELF.fetch(
			`https://example.com/deployments/${bubble.deployment_id}/subscribe`,
			{
				headers: {
					authorization: `Bearer ${bubble.api_key}`,
					cookie: "session=client-cookie",
					Upgrade: "websocket",
				},
			},
		);

		expect(response.status).toBe(101);
		expect(response.webSocket).toBeTruthy();
		response.webSocket?.accept();
		response.webSocket?.close();
	});

	it("fails closed when INTERNAL_DO_SECRET is unset", async () => {
		const envWithoutSecret = { ...env, INTERNAL_DO_SECRET: "" };
		await expect(worker.fetch(
			new Request("https://example.com/deployments", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ name: "missing-secret", subscriptions: ["missing:secret"] }),
			}),
			envWithoutSecret,
			{} as ExecutionContext,
		)).rejects.toThrow("DeploymentSession fetch failed with status 403");
	});

	it("does not proxy raw client requests to DeploymentSession", async () => {
		// @ts-expect-error Vite supplies ?raw imports in the Vitest transform.
		const source = await import("../src/index?raw");
		expect(source.default).not.toMatch(/stub\.fetch\(\s*request\s*\)/);
		expect(source.default).not.toMatch(/(?:loopStub|pStub)\.fetch\(/);
		expect(source.default).not.toMatch(/return stub\.fetch\(\s*internalEventRequest/);
		expect(source.default).toContain("fetchDeploymentSession(");
		expect(source.default).toContain("stub.fetch(internalWebSocketRequest");
	});
});

// ---------------------------------------------------------------------------
// #488 — resource-grant authorization through the Worker (KV) routes.
// ---------------------------------------------------------------------------
describe("#488 resource-grant authorization (route + delivery)", () => {
	let seq = 0;

	async function githubIssue(repo: string): Promise<number> {
		const res = await SELF.fetch("https://example.com/webhooks/github", {
			method: "POST",
			headers: {
				"content-type": "application/json",
				"x-github-event": "issues",
				"x-github-delivery": `dlv-${seq++}`,
			},
			body: JSON.stringify({
				action: "opened",
				repository: { full_name: repo },
				sender: { login: "u" },
				issue: { number: 1, title: "t", state: "open", html_url: `https://github.com/${repo}/issues/1` },
			}),
		});
		expect(res.status).toBe(200);
		return (await res.json() as { delivered_to: number }).delivered_to;
	}

	it("rejects an unsigned /resources/authorize with 403 (mandatory auth, test 1)", async () => {
		const res = await SELF.fetch("https://example.com/resources/authorize", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ service: "github", resource: "org/repo", credential: "ghp_x" }),
		});
		expect(res.status).toBe(403);
	});

	it("rejects /resources/authorize with a bad signature (403, test 1)", async () => {
		const b = await mintBubble(["_bootstrap"]);
		const body = JSON.stringify({ service: "github", resource: "org/repo", credential: "ghp_x" });
		const headers = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/resources/authorize", body);
		headers["x-moda-signature"] = "deadbeef";
		const res = await SELF.fetch("https://example.com/resources/authorize", {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body,
		});
		expect(res.status).toBe(403);
	});

	it("registration hard-rejects an ungranted global topic (400 unauthorized_topics, test 5)", async () => {
		const b = await mintBubble(["_bootstrap"]);
		const repo = `org/reject-${seq++}`;
		const body = JSON.stringify({ name: `d-${seq++}`, subscriptions: [`github:${repo}`] });
		const headers = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/deployments", body);
		const res = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json", ...headers },
			body,
		});
		expect(res.status).toBe(400);
		const j = await res.json() as { error: string; topics: string[] };
		expect(j.error).toBe("unauthorized_topics");
		expect(j.topics).toEqual([`github:${repo}`]);
	});

	it("delivery drops a STALE index entry for a grant-less bubble (headline AC, test 6)", async () => {
		const repo = `org${seq++}/repo`;
		// A legitimately granted + registered deployment.
		await deploymentWithGrants([`github:${repo}`]);

		// Inject a STALE subscription-index entry directly into KV: a second
		// deployment in a DIFFERENT bubble that holds NO grant (simulating an
		// entry written before enforcement, or a forged one).
		const staleBubble = `bub_stale_${seq++}`;
		const staleId = `dep_stale_${seq++}`;
		await env.EVENTS.put(`deployment_id:${staleId}`, JSON.stringify({
			id: staleId, name: "stale", api_key: `k_${staleId}`, bubble_id: staleBubble,
			subscriptions: [`github:${repo}`],
		}));
		const subKey = `subscriptions:github:${repo}`;
		const existing = await env.EVENTS.get(subKey);
		const ids: string[] = existing ? JSON.parse(existing) : [];
		ids.push(staleId);
		await env.EVENTS.put(subKey, JSON.stringify(ids));

		// Only the granted deployment receives the event — the stale entry is dropped.
		expect(await githubIssue(repo)).toBe(1);
	});

	it("multi-bubble: two granted bubbles both receive; a third without a grant gets none (test 7)", async () => {
		const repo = `org${seq++}/repo`;
		await deploymentWithGrants([`github:${repo}`]); // bubble 1
		await deploymentWithGrants([`github:${repo}`]); // bubble 2

		// A third bubble subscribes via a stale index entry but holds no grant.
		const staleId = `dep_nogrant_${seq++}`;
		await env.EVENTS.put(`deployment_id:${staleId}`, JSON.stringify({
			id: staleId, name: "nogrant", api_key: `k_${staleId}`, bubble_id: `bub_ng_${seq++}`,
			subscriptions: [`github:${repo}`],
		}));
		const subKey = `subscriptions:github:${repo}`;
		const existing = await env.EVENTS.get(subKey);
		const ids: string[] = existing ? JSON.parse(existing) : [];
		ids.push(staleId);
		await env.EVENTS.put(subKey, JSON.stringify(ids));

		expect(await githubIssue(repo)).toBe(2); // exactly the two granted bubbles
	}, 15_000);

	it("slack: a signed workspace registration writes the slack grant, gating delivery (test 8)", async () => {
		const b = await mintBubble(["_bootstrap"]);
		const team = `T_GRANT_${seq++}`;
		const chan = `C_${seq++}`;
		stubSlackFetch(team, "B1", "A1");

		// Signed workspace registration → writes the slack grant for this bubble.
		const regBody = JSON.stringify({ workspace_id: team, bot_token: "xoxb-x", bot_id: "B1", app_id: "A1" });
		const regHeaders = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/slack/workspaces", regBody);
		const reg = await SELF.fetch("https://example.com/slack/workspaces", {
			method: "POST",
			headers: { "content-type": "application/json", ...regHeaders },
			body: regBody,
		});
		expect(reg.status).toBe(200);

		// JOIN-register a deployment subscribing to the channel topic — passes the
		// gate because the signed registration already granted slack:{team}.
		const depBody = JSON.stringify({ name: `slackdep-${seq++}`, subscriptions: [`slack:${team}:${chan}`] });
		const depHeaders = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/deployments", depBody);
		const depRes = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json", ...depHeaders },
			body: depBody,
		});
		expect(depRes.status).toBe(201);

		// An inbound slack message to that channel is delivered to the granted dep.
		const msg = await SELF.fetch("https://example.com/webhooks/slack", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				type: "event_callback", team_id: team,
				event: { type: "app_mention", channel: chan, user: "U1", text: "hi", ts: `170${seq++}.0001` },
			}),
		});
		expect(msg.status).toBe(200);
		expect((await msg.json() as { delivered_to: number }).delivered_to).toBe(1);
	});

	it("slack: signed workspace registration denies a token for a different team", async () => {
		const b = await mintBubble(["_bootstrap"]);
		stubSlackFetch("T_OTHER", "B1", "A1");
		const regBody = JSON.stringify({ workspace_id: "T_VICTIM", bot_token: "xoxb-fake", bot_id: "B_FAKE", app_id: "A_FAKE" });
		const regHeaders = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/slack/workspaces", regBody);
		const reg = await SELF.fetch("https://example.com/slack/workspaces", {
			method: "POST",
			headers: { "content-type": "application/json", ...regHeaders },
			body: regBody,
		});
		expect(reg.status).toBe(403);

		const depBody = JSON.stringify({ name: `slack-denied-${seq++}`, subscriptions: ["slack:T_VICTIM:C1"] });
		const depHeaders = await bubbleHeaders(b.bubble_id, b.bubble_key, "POST", "/deployments", depBody);
		const depRes = await SELF.fetch("https://example.com/deployments", {
			method: "POST",
			headers: { "content-type": "application/json", ...depHeaders },
			body: depBody,
		});
		expect(depRes.status).toBe(400);
	});
});
