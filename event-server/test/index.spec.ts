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

	// Auth endpoints
	describe("auth", () => {
		it("GET /auth/config returns client_id", async () => {
			const response = await SELF.fetch("https://example.com/auth/config");
			expect(response.status).toBe(200);
			const body = await response.json() as { client_id: string };
			expect("client_id" in body).toBe(true);
		});

		it("POST /auth/github/callback rejects missing fields", async () => {
			const response = await SELF.fetch("https://example.com/auth/github/callback", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ code: "abc" }),
			});
			expect(response.status).toBe(400);
		});

		it("POST /auth/github/callback rejects invalid JSON", async () => {
			const response = await SELF.fetch("https://example.com/auth/github/callback", {
				method: "POST",
				body: "not json",
			});
			expect(response.status).toBe(400);
		});
	});

	// Deployment deletion
	describe("DELETE /deployments/:id", () => {
		it("rejects without auth", async () => {
			const response = await SELF.fetch("https://example.com/deployments/some-id", {
				method: "DELETE",
			});
			expect(response.status).toBe(401);
		});

		it("rejects with invalid api key", async () => {
			const response = await SELF.fetch("https://example.com/deployments/some-id", {
				method: "DELETE",
				headers: { authorization: "Bearer invalid_key" },
			});
			expect(response.status).toBe(403);
		});

		it("deletes a deployment with valid api key", async () => {
			// Register first
			const regResp = await SELF.fetch("https://example.com/deployments", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					name: "to-delete",
					subscriptions: ["moda-labs/test"],
				}),
			});
			expect(regResp.status).toBe(201);
			const { deployment_id, api_key } = await regResp.json() as {
				deployment_id: string;
				api_key: string;
			};

			// Delete
			const delResp = await SELF.fetch(
				`https://example.com/deployments/${deployment_id}`,
				{
					method: "DELETE",
					headers: { authorization: `Bearer ${api_key}` },
				},
			);
			expect(delResp.status).toBe(200);
			const body = await delResp.json() as { deleted: boolean };
			expect(body.deleted).toBe(true);
		});

		it("returns 404 for non-existent deployment", async () => {
			// Register to get a valid key, then try to delete a different ID
			const regResp = await SELF.fetch("https://example.com/deployments", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					name: "holder",
					subscriptions: ["moda-labs/test"],
				}),
			});
			const { api_key } = await regResp.json() as { api_key: string };

			const delResp = await SELF.fetch(
				"https://example.com/deployments/nonexistent-id",
				{
					method: "DELETE",
					headers: { authorization: `Bearer ${api_key}` },
				},
			);
			// API key doesn't match this deployment ID
			expect(delResp.status).toBe(403);
		});
	});
});
