import { SELF } from "cloudflare:test";
import { describe, it, expect } from "vitest";

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
