import { SELF, env } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import worker from "../src/index";

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

	it("accepts generic topic event", async () => {
		const response = await SELF.fetch("https://example.com/events/deploy.complete", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				source: "ci",
				payload: { status: "success", sha: "abc123" },
			}),
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

	it("rejects slack send without channel or text", async () => {
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ text: "hello" }),
		});
		expect(response.status).toBe(400);
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
		// Register a workspace with a token so the handler reaches sendSlackMessage
		const regResponse = await SELF.fetch("https://example.com/slack/workspaces", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				workspace_id: "T_FAIL",
				bot_token: "xoxb-fake-token",
			}),
		});
		expect(regResponse.status).toBe(200);

		// Now send — the fetch to slack.com will fail in the test sandbox
		const response = await SELF.fetch("https://example.com/slack/send", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				workspace: "T_FAIL",
				channel: "C123",
				text: "hello",
			}),
		});

		// Should get 502, not 500 — the try/catch maps fetch failures to 502
		expect(response.status).toBe(502);
		const body = await response.json() as { ok: boolean; error: string };
		expect(body.ok).toBe(false);
		expect(body.error).toBeTruthy();
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
