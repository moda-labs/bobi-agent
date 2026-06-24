export { DeploymentSession } from "./deployment-session";
import {
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type SlackWorkspaceRecord,
	type NormalizedEvent,
	type HandlerResult,
	authenticateDeployment,
	subscriptionKeysForEvent,
	verifyGitHubSignature,
	verifySlackSignature,
	readBubbleAuthHeaders,
	handleGitHubWebhook,
	handleLinearWebhook,
	handleSlackWebhook,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleDeregisterDeployment,
	handleTopicEvent,
	handleSlackSend,
	handleSlackWorkspaceRegister,
	getAuthRejectionCounters,
} from "./core";
import {
	isExemptFromBreaker,
	recordDelivery,
	drainPaused,
	conversationKey,
	buildLoopDetectedEvent,
} from "./circuit-breaker";

interface Env {
	EVENTS: KVNamespace;
	DEPLOYMENT_SESSION: DurableObjectNamespace;
	WEBHOOK_SECRET?: string;
	SLACK_SIGNING_SECRET?: string;
}

// ---------------------------------------------------------------------------
// KV + Durable Objects storage adapter
// ---------------------------------------------------------------------------

function createKVStorage(env: Env): StorageAdapter {
	return {
		async getDeploymentByApiKey(apiKey: string): Promise<DeploymentRecord | null> {
			const data = await env.EVENTS.get(`deployments:${apiKey}`);
			return data ? JSON.parse(data) : null;
		},

		async getDeploymentByName(name: string, bubbleId: string): Promise<DeploymentRecord | null> {
			const data = await env.EVENTS.get(`deployment_name:${bubbleId}:${name}`);
			return data ? JSON.parse(data) : null;
		},

		async putDeployment(deployment: DeploymentRecord): Promise<void> {
			const json = JSON.stringify(deployment);
			await env.EVENTS.put(`deployments:${deployment.api_key}`, json);
			await env.EVENTS.put(`deployment_id:${deployment.id}`, json);
			await env.EVENTS.put(`deployment_name:${deployment.bubble_id}:${deployment.name}`, json);
		},

		async getBubble(bubbleId: string): Promise<BubbleRecord | null> {
			const data = await env.EVENTS.get(`bubble:${bubbleId}`);
			return data ? JSON.parse(data) : null;
		},

		async putBubble(bubble: BubbleRecord): Promise<void> {
			await env.EVENTS.put(`bubble:${bubble.id}`, JSON.stringify(bubble));
		},

		async removeDeployment(deployment: DeploymentRecord): Promise<void> {
			await env.EVENTS.delete(`deployments:${deployment.api_key}`);
			await env.EVENTS.delete(`deployment_id:${deployment.id}`);
			await env.EVENTS.delete(`deployment_name:${deployment.bubble_id}:${deployment.name}`);
		},

		async addSubscription(key: string, deploymentId: string): Promise<void> {
			const kvKey = `subscriptions:${key}`;
			const existing = await env.EVENTS.get(kvKey);
			const ids: string[] = existing ? JSON.parse(existing) : [];
			if (!ids.includes(deploymentId)) {
				ids.push(deploymentId);
				await env.EVENTS.put(kvKey, JSON.stringify(ids));
			}
		},

		async removeSubscription(key: string, deploymentId: string): Promise<void> {
			const kvKey = `subscriptions:${key}`;
			const existing = await env.EVENTS.get(kvKey);
			if (!existing) return;
			const ids: string[] = JSON.parse(existing);
			const filtered = ids.filter((id) => id !== deploymentId);
			if (filtered.length === 0) {
				await env.EVENTS.delete(kvKey);
			} else {
				await env.EVENTS.put(kvKey, JSON.stringify(filtered));
			}
		},

		async deliver(event: NormalizedEvent): Promise<number> {
			const keys = subscriptionKeysForEvent(event);
			const ids = new Set<string>();
			const lookups = await Promise.all(
				keys.map((k) => env.EVENTS.get(`subscriptions:${k}`)),
			);
			for (const data of lookups) {
				if (data) {
					for (const id of JSON.parse(data)) ids.add(id);
				}
			}

			const exempt = isExemptFromBreaker(event);
			const allowedIds: string[] = [];

			for (const depId of ids) {
				if (!exempt) {
					const verdict = recordDelivery(depId, event);
					if (verdict.justTripped) {
						const convKey = conversationKey(event)!;
						const loopEvent = buildLoopDetectedEvent(depId, convKey, event);
						const loopDoId = env.DEPLOYMENT_SESSION.idFromName(depId);
						const loopStub = env.DEPLOYMENT_SESSION.get(loopDoId);
						loopStub.fetch(
							new Request("https://internal/event", {
								method: "POST",
								body: JSON.stringify(loopEvent),
							}),
						);
					}
					if (!verdict.allow) continue;

					// Human event may have unpaused — drain buffered events
					const drained = drainPaused(depId, event);
					for (const paused of drained) {
						const pDoId = env.DEPLOYMENT_SESSION.idFromName(depId);
						const pStub = env.DEPLOYMENT_SESSION.get(pDoId);
						pStub.fetch(
							new Request("https://internal/event", {
								method: "POST",
								body: JSON.stringify(paused),
							}),
						);
					}
				}
				allowedIds.push(depId);
			}

			await Promise.all(
				allowedIds.map((depId) => {
					const doId = env.DEPLOYMENT_SESSION.idFromName(depId);
					const stub = env.DEPLOYMENT_SESSION.get(doId);
					return stub.fetch(
						new Request("https://internal/event", {
							method: "POST",
							body: JSON.stringify(event),
						}),
					);
				}),
			);
			return allowedIds.length;
		},

		async getSlackWorkspace(workspaceId: string): Promise<SlackWorkspaceRecord | null> {
			const data = await env.EVENTS.get(`slack_workspace:${workspaceId}`);
			return data ? JSON.parse(data) : null;
		},

		async putSlackWorkspace(workspaceId: string, record: SlackWorkspaceRecord): Promise<void> {
			await env.EVENTS.put(`slack_workspace:${workspaceId}`, JSON.stringify(record));
		},

		async initDeploymentSession(deploymentId: string, subscriptions: string[]): Promise<void> {
			const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
			const stub = env.DEPLOYMENT_SESSION.get(doId);
			await stub.fetch(
				new Request("https://internal/init", {
					method: "POST",
					body: JSON.stringify({ deployment_id: deploymentId, subscriptions }),
				}),
			);
		},
	};
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function respond(result: HandlerResult): Response {
	return Response.json(result.body, { status: result.status });
}

async function readJson(request: Request): Promise<Record<string, unknown> | null> {
	try {
		return (await request.json()) as Record<string, unknown>;
	} catch {
		return null;
	}
}

// ---------------------------------------------------------------------------
// Routing table
// ---------------------------------------------------------------------------

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const path = url.pathname;
		const method = request.method;
		const storage = createKVStorage(env);

		if (method === "GET" && path === "/health") {
			return Response.json({
				status: "ok",
				auth: "hmac",
				rejections: getAuthRejectionCounters(),
			});
		}

		if (method === "POST" && path === "/webhooks/github") {
			const body = await request.text();

			if (env.WEBHOOK_SECRET) {
				const sigHeader = request.headers.get("x-hub-signature-256") || "";
				const valid = await verifyGitHubSignature(env.WEBHOOK_SECRET, new TextEncoder().encode(body), sigHeader);
				if (!valid) {
					return Response.json({ error: "invalid signature" }, { status: 401 });
				}
			}

			let payload: Record<string, unknown>;
			try {
				payload = JSON.parse(body);
			} catch {
				return Response.json({ error: "invalid JSON" }, { status: 400 });
			}
			const eventHeader = request.headers.get("x-github-event") || "unknown";
			const deliveryId = request.headers.get("x-github-delivery") || crypto.randomUUID();
			return respond(await handleGitHubWebhook(storage, eventHeader, deliveryId, payload));
		}

		if (method === "POST" && path === "/webhooks/linear") {
			const payload = await readJson(request);
			if (!payload) return Response.json({ error: "invalid JSON" }, { status: 400 });
			return respond(await handleLinearWebhook(storage, payload));
		}

		if (method === "POST" && path === "/webhooks/slack") {
			const body = await request.text();

			let payload: Record<string, unknown>;
			try {
				payload = JSON.parse(body);
			} catch {
				return Response.json({ error: "invalid JSON" }, { status: 400 });
			}

			// url_verification must be handled before BOTH the retry short-circuit
			// and the signature check: it carries no signing headers, and Slack
			// retries a failed handshake with x-slack-retry-num set — so swallowing
			// retries here would leave the request URL permanently unverified.
			if (payload.type === "url_verification") {
				return Response.json({ challenge: payload.challenge });
			}

			// Dedup retried EVENT deliveries so the agent doesn't double-process.
			if (request.headers.get("x-slack-retry-num")) {
				return Response.json({ ok: true });
			}

			if (env.SLACK_SIGNING_SECRET) {
				const timestamp = request.headers.get("x-slack-request-timestamp") || "";
				const signature = request.headers.get("x-slack-signature") || "";
				const valid = await verifySlackSignature(env.SLACK_SIGNING_SECRET, timestamp, body, signature);
				if (!valid) {
					return Response.json({ error: "invalid signature" }, { status: 401 });
				}
			}

			return respond(await handleSlackWebhook(storage, payload));
		}

		if (method === "POST" && path === "/deployments") {
			// Raw text (not readJson) so the join signature verifies over the
			// exact transmitted bytes.
			const raw = await request.text();
			let data: Record<string, unknown>;
			try {
				data = JSON.parse(raw);
			} catch {
				return Response.json({ error: "invalid JSON" }, { status: 400 });
			}
			const ctx = readBubbleAuthHeaders(
				(n) => request.headers.get(n),
				method,
				url.pathname + url.search,
				raw,
			);
			return respond(await handleRegisterDeployment(storage, data, ctx));
		}

		// WebSocket subscribe — transport-specific (Durable Object proxy)
		if (method === "GET" && path.startsWith("/deployments/") && path.endsWith("/subscribe")) {
			const match = path.match(/^\/deployments\/([^/]+)\/subscribe$/);
			if (!match) return new Response("Invalid path", { status: 400 });
			const deploymentId = match[1];

			const authHeader = request.headers.get("authorization");
			if (!authHeader?.startsWith("Bearer ")) {
				return new Response("Unauthorized", { status: 401 });
			}
			const apiKey = authHeader.slice(7);
			const deployment = await authenticateDeployment(storage, apiKey, deploymentId);
			if (!deployment) {
				return new Response("Invalid API key", { status: 403 });
			}

			const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
			const stub = env.DEPLOYMENT_SESSION.get(doId);
			return stub.fetch(request);
		}

		if (method === "PUT" && path.startsWith("/deployments/") && path.endsWith("/subscriptions")) {
			const match = path.match(/^\/deployments\/([^/]+)\/subscriptions$/);
			if (!match) return Response.json({ error: "invalid path" }, { status: 400 });
			const deploymentId = match[1];

			const authHeader = request.headers.get("authorization");
			if (!authHeader?.startsWith("Bearer ")) {
				return Response.json({ error: "unauthorized" }, { status: 401 });
			}
			const apiKey = authHeader.slice(7);

			const body = await readJson(request);
			if (!body) return Response.json({ error: "invalid JSON" }, { status: 400 });

			return respond(await handleUpdateSubscriptions(storage, deploymentId, apiKey, body));
		}

		if (method === "DELETE" && path.startsWith("/deployments/")) {
			const match = path.match(/^\/deployments\/([^/]+)$/);
			if (!match) return Response.json({ error: "invalid path" }, { status: 400 });
			const deploymentId = match[1];

			const authHeader = request.headers.get("authorization");
			if (!authHeader?.startsWith("Bearer ")) {
				return Response.json({ error: "unauthorized" }, { status: 403 });
			}
			const apiKey = authHeader.slice(7);

			return respond(await handleDeregisterDeployment(storage, deploymentId, apiKey));
		}

		// Generic topic: POST /events/{topic}
		const topicMatch = method === "POST" && path.match(/^\/events\/(.+)$/);
		if (topicMatch) {
			// Raw text so the publish signature verifies over the exact bytes.
			const raw = await request.text();
			let data: Record<string, unknown>;
			try {
				data = JSON.parse(raw);
			} catch {
				return Response.json({ error: "invalid JSON" }, { status: 400 });
			}
			const ctx = readBubbleAuthHeaders(
				(n) => request.headers.get(n),
				method,
				url.pathname + url.search,
				raw,
			);
			return respond(await handleTopicEvent(storage, topicMatch[1], data, ctx));
		}

		if (method === "POST" && path === "/slack/send") {
			const body = await readJson(request);
			if (!body) return Response.json({ error: "invalid JSON" }, { status: 400 });
			return respond(await handleSlackSend(storage, body));
		}

		if (method === "POST" && path === "/slack/workspaces") {
			const body = await readJson(request);
			if (!body) return Response.json({ error: "invalid JSON" }, { status: 400 });
			return respond(await handleSlackWorkspaceRegister(storage, body));
		}

		return new Response("Not Found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;
