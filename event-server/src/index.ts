export { DeploymentSession } from "./deployment-session";
import {
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type SlackWorkspaceRecord,
	type ResourceGrant,
	type NormalizedEvent,
	type HandlerResult,
	authenticateDeployment,
	subscriptionKeysForEvent,
	admittedDeploymentIds,
	handleAuthorizeResource,
	readBubbleAuthHeaders,
	hasBubbleSignature,
	hasPartialBubbleSignature,
	authenticateBubble,
	handleWebhookRequest,
	handleWebhookHandshake,
	matchWebhookSource,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleDeregisterDeployment,
	handleTopicEvent,
	handleChannelsSend,
	handleChannelsTyping,
	handleChannelsHistory,
	handleSlackSend,
	handleSlackWorkspaceRegister,
	handleWhatsAppNumberRegister,
	handleTestSeedResourceGrants,
	getAuthRejectionCounters,
} from "./core";
import {
	isExemptFromBreaker,
	recordDelivery,
	drainPaused,
	conversationKey,
	buildLoopDetectedEvent,
} from "./circuit-breaker";
import { internalEventRequest, internalWebSocketRequest, publicBearerFromWebSocketProtocols } from "./internal-auth";

interface Env {
	EVENTS: KVNamespace;
	DEPLOYMENT_SESSION: DurableObjectNamespace;
	INTERNAL_DO_SECRET: string;
	WEBHOOK_SECRET?: string;
	SLACK_SIGNING_SECRET?: string;
	LINEAR_WEBHOOK_SECRET?: string;
	WHATSAPP_APP_SECRET?: string;
	WHATSAPP_VERIFY_TOKEN?: string;
	TEST_GRANTS_SECRET?: string;
}

// ---------------------------------------------------------------------------
// KV + Durable Objects storage adapter
// ---------------------------------------------------------------------------

function createKVStorage(env: Env): StorageAdapter {
	const adapter: StorageAdapter = {
		async getDeploymentByApiKey(apiKey: string): Promise<DeploymentRecord | null> {
			const data = await env.EVENTS.get(`deployments:${apiKey}`);
			return data ? JSON.parse(data) : null;
		},

		async getDeploymentByName(name: string, bubbleId: string): Promise<DeploymentRecord | null> {
			const data = await env.EVENTS.get(`deployment_name:${bubbleId}:${name}`);
			return data ? JSON.parse(data) : null;
		},

		async getDeploymentById(id: string): Promise<DeploymentRecord | null> {
			const data = await env.EVENTS.get(`deployment_id:${id}`);
			return data ? JSON.parse(data) : null;
		},

		async putResourceGrant(grant: ResourceGrant): Promise<void> {
			await env.EVENTS.put(
				`resource_grant:${grant.service}:${grant.resource}:${grant.bubble_id}`,
				JSON.stringify(grant),
			);
			// Per-bubble index for deregister/observability (best-effort accrete).
			const idxKey = `resource_grants_for_bubble:${grant.bubble_id}`;
			const existing = await env.EVENTS.get(idxKey);
			const ids: string[] = existing ? JSON.parse(existing) : [];
			if (!ids.includes(grant.id)) {
				ids.push(grant.id);
				await env.EVENTS.put(idxKey, JSON.stringify(ids));
			}
		},

		async hasResourceGrant(service: string, resource: string, bubbleId: string): Promise<boolean> {
			const data = await env.EVENTS.get(`resource_grant:${service}:${resource}:${bubbleId}`);
			return data !== null;
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
			// Enforcement layer 2 (#488): admittedDeploymentIds applies the live
			// resource-grant filter to GLOBAL topics, so a stale subscription-index
			// entry for a bubble that no longer (or never) held a grant is dropped
			// here — delivery is the authoritative, fail-closed boundary.
			const ids = await admittedDeploymentIds(adapter, event, async (k) => {
				const data = await env.EVENTS.get(`subscriptions:${k}`);
				return data ? (JSON.parse(data) as string[]) : [];
			});

			const exempt = isExemptFromBreaker(event);
			const allowedIds: string[] = [];
			const sideDeliveries: Promise<Response>[] = [];

			for (const depId of ids) {
				if (!exempt) {
					const verdict = recordDelivery(depId, event);
					if (verdict.justTripped) {
						const convKey = conversationKey(event)!;
						const loopEvent = buildLoopDetectedEvent(depId, convKey, event);
						const loopDoId = env.DEPLOYMENT_SESSION.idFromName(depId);
						const loopStub = env.DEPLOYMENT_SESSION.get(loopDoId);
						sideDeliveries.push(fetchDeploymentSession(
							loopStub,
							internalEventRequest(env, "https://internal/event", JSON.stringify(loopEvent)),
						));
					}
					if (!verdict.allow) continue;

					// Human event may have unpaused — drain buffered events
					const drained = drainPaused(depId, event);
					for (const paused of drained) {
						const pDoId = env.DEPLOYMENT_SESSION.idFromName(depId);
						const pStub = env.DEPLOYMENT_SESSION.get(pDoId);
						sideDeliveries.push(fetchDeploymentSession(
							pStub,
							internalEventRequest(env, "https://internal/event", JSON.stringify(paused)),
						));
					}
				}
				allowedIds.push(depId);
			}

			await Promise.all(
				[
					...sideDeliveries,
					...allowedIds.map((depId) => {
						const doId = env.DEPLOYMENT_SESSION.idFromName(depId);
						const stub = env.DEPLOYMENT_SESSION.get(doId);
						return fetchDeploymentSession(
							stub,
							internalEventRequest(env, "https://internal/event", JSON.stringify(event)),
						);
					}),
				],
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

		async getChannelState(key: string): Promise<Record<string, unknown> | null> {
			const data = await env.EVENTS.get(`channel_state:${key}`);
			return data ? JSON.parse(data) : null;
		},

		async putChannelState(key: string, value: Record<string, unknown>): Promise<void> {
			await env.EVENTS.put(`channel_state:${key}`, JSON.stringify(value));
		},

		async initDeploymentSession(deploymentId: string, subscriptions: string[]): Promise<void> {
			const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
			const stub = env.DEPLOYMENT_SESSION.get(doId);
			await fetchDeploymentSession(
				stub,
				internalEventRequest(
					env,
					"https://internal/init",
					JSON.stringify({ deployment_id: deploymentId, subscriptions }),
				),
			);
		},
	};
	return adapter;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function respond(result: HandlerResult): Response {
	return Response.json(result.body, { status: result.status });
}

async function fetchDeploymentSession(
	session: DurableObjectStub,
	internalRequest: Request,
	expectedStatus = 200,
): Promise<Response> {
	const response = await session.fetch(internalRequest);
	if (response.status !== expectedStatus) {
		throw new Error(`DeploymentSession fetch failed with status ${response.status}`);
	}
	return response;
}

async function readJson(request: Request): Promise<Record<string, unknown> | null> {
	try {
		return (await request.json()) as Record<string, unknown>;
	} catch {
		return null;
	}
}

// Shared prologue of every mandatory bubble-signed route: read the exact wire
// bytes (the signature covers them — never a re-serialization), parse JSON,
// and authenticate. Returns a ready error Response on failure. A GET carries
// an empty body, which verifies and parses as {}.
async function bubbleAuthedJson(
	request: Request,
	url: URL,
	storage: StorageAdapter,
): Promise<{ bubble: BubbleRecord; data: Record<string, unknown> } | Response> {
	const raw = await request.text();
	let data: Record<string, unknown> = {};
	if (raw) {
		try {
			data = JSON.parse(raw) as Record<string, unknown>;
		} catch {
			return Response.json({ error: "invalid JSON" }, { status: 400 });
		}
	}
	const ctx = readBubbleAuthHeaders(
		(n) => request.headers.get(n),
		request.method,
		url.pathname + url.search,
		raw,
	);
	const bubble = await authenticateBubble(storage, ctx);
	if (!bubble) return Response.json({ error: "forbidden" }, { status: 403 });
	return { bubble, data };
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

		// Inbound webhooks — one pipeline for every source (#639): the shared
		// core matches the route, verifies the exact wire bytes (the verify
		// slot is structural — no source registers without one), normalizes,
		// and delivers. matchWebhookSource returns null for an unregistered
		// path, which 404s below WITHOUT consuming the request body.
		const webhookSecrets = {
			github: env.WEBHOOK_SECRET,
			slack: env.SLACK_SIGNING_SECRET,
			linear: env.LINEAR_WEBHOOK_SECRET,
			whatsapp: env.WHATSAPP_APP_SECRET,
			whatsappVerifyToken: env.WHATSAPP_VERIFY_TOKEN,
		};

		// Provider GET handshakes (#656): Meta verifies a webhook URL with a
		// GET whose challenge must echo back as RAW text (JSON-quoting breaks
		// its byte comparison), so this responds outside respond()/JSON.
		if (method === "GET") {
			const handshakeSource = matchWebhookSource(path);
			if (handshakeSource) {
				const h = handleWebhookHandshake(
					handshakeSource, (n) => url.searchParams.get(n) || "", webhookSecrets);
				if (h) {
					return new Response(h.text, {
						status: h.status,
						headers: { "Content-Type": "text/plain" },
					});
				}
			}
		}

		const webhookSource = method === "POST" ? matchWebhookSource(path) : null;
		if (webhookSource) {
			const rawBody = await request.text();
			const result = await handleWebhookRequest(
				storage,
				webhookSource,
				{ rawBody, header: (n) => request.headers.get(n) || "" },
				webhookSecrets,
			);
			if (result) return respond(result);
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
			const bearerFromHeader = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : "";
			const bearerFromProtocol = publicBearerFromWebSocketProtocols(
				request.headers.get("sec-websocket-protocol"),
			) || "";
			const apiKey = bearerFromHeader || bearerFromProtocol;
			if (!apiKey) {
				return new Response("Unauthorized", { status: 401 });
			}
			const deployment = await authenticateDeployment(storage, apiKey, deploymentId);
			if (!deployment) {
				return new Response("Invalid API key", { status: 403 });
			}

			const doId = env.DEPLOYMENT_SESSION.idFromName(deploymentId);
			const stub = env.DEPLOYMENT_SESSION.get(doId);
			return stub.fetch(internalWebSocketRequest(env, request));
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
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleSlackSend(storage, auth.data, auth.bubble.id));
		}

		if (method === "POST" && path === "/channels/send") {
			// Channel-agnostic send (#618) - same mandatory bubble auth as
			// /slack/send.
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleChannelsSend(storage, auth.data, auth.bubble.id));
		}

		if (method === "POST" && path === "/channels/typing") {
			// Set/clear a channel's thinking indicator (#629).
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleChannelsTyping(storage, auth.data, auth.bubble.id));
		}

		if (method === "GET" && path === "/channels/history") {
			// Read a conversation's messages (#629). The signature covers the
			// full path INCLUDING the query string, with an empty body.
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			const conversation = url.searchParams.get("conversation") || "";
			const limit = parseInt(url.searchParams.get("limit") || "100", 10);
			return respond(await handleChannelsHistory(storage, conversation, limit, auth.bubble.id));
		}

		if (method === "POST" && path === "/resources/authorize") {
			// Auth is MANDATORY (no legacy unsigned caller) — mirror /slack/send.
			// The credential in the body is verified once and never persisted; the
			// route is deliberately excluded from any body logging.
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleAuthorizeResource(storage, auth.data, auth.bubble.id));
		}

		if (method === "POST" && path === "/__test/resource-grants" && env.TEST_GRANTS_SECRET) {
			if (request.headers.get("x-moda-test-secret") !== env.TEST_GRANTS_SECRET) {
				return Response.json({ error: "not found" }, { status: 404 });
			}
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
			const bubble = await authenticateBubble(storage, ctx);
			if (!bubble) return Response.json({ error: "not found" }, { status: 404 });
			return respond(await handleTestSeedResourceGrants(storage, data, bubble.id));
		}

		if (method === "POST" && path === "/slack/workspaces") {
			// Raw text so an (optional) bubble signature verifies over exact bytes.
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
			// Auth is OPTIONAL here: an unsigned registration still writes the
			// global record (inbound self-reply loop prevention, kept for legacy
			// clients). A signed one ALSO writes the bubble-scoped record that
			// outbound /slack/send reads. A present-but-invalid signature is a
			// malformed/forged request — reject rather than silently downgrade.
			let bubbleId: string | undefined;
			if (hasBubbleSignature(ctx)) {
				const bubble = await authenticateBubble(storage, ctx);
				if (!bubble) return Response.json({ error: "forbidden" }, { status: 403 });
				bubbleId = bubble.id;
			} else if (hasPartialBubbleSignature(ctx)) {
				return Response.json({ error: "forbidden" }, { status: 403 });
			}
			return respond(await handleSlackWorkspaceRegister(storage, data, bubbleId));
		}

		if (method === "POST" && path === "/whatsapp/numbers") {
			// Signed-only (#656): unlike Slack there is no unsigned global-record
			// use case, so an unauthenticated registration is rejected outright.
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
			if (!hasBubbleSignature(ctx)) {
				return Response.json({ error: "forbidden" }, { status: 403 });
			}
			const bubble = await authenticateBubble(storage, ctx);
			if (!bubble) return Response.json({ error: "forbidden" }, { status: 403 });
			return respond(await handleWhatsAppNumberRegister(storage, data, bubble.id));
		}

		return new Response("Not Found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;
