export { DeploymentSession } from "./deployment-session";
import {
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type SlackWorkspaceRecord,
	type ResourceGrant,
	type IngestTokenRecord,
	type WebhookDeliveryRecord,
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
	handleIngestTokenCreate,
	handleIngestTokenList,
	handleIngestTokenRevoke,
	handleChannelsSend,
	handleChannelsTyping,
	handleChannelsHistory,
	handleSlackWorkspaceRegister,
	handleWhatsAppNumberRegister,
	handleTestSeedResourceGrants,
	getAuthRejectionCounters,
	createTopicEvent,
} from "@moda-labs/bobi-events-core";
import {
	isExemptFromBreaker,
	recordDelivery,
	drainPaused,
	conversationKey,
	buildLoopDetectedEvent,
} from "@moda-labs/bobi-events-core/circuit-breaker";
import { internalEventRequest, internalWebSocketRequest, publicBearerFromWebSocketProtocols } from "./internal-auth";
import {
	type FleetSnapshot,
	type LifecyclePayload,
	type CommandResultPayload,
	buildFleetStatus,
	buildInstanceDetail,
	buildCommandView,
	createFleetKVStorage,
	foldHeartbeat,
	foldLifecycle,
	foldCommandResult,
	isAdminCommand,
	ADMIN_COMMANDS,
	issueAdminCommand,
	type AdminPublisher,
	type IssueFailure,
	COMMAND_RESULT_TOPIC,
	requireOperator,
	windowsFromEnv,
} from "./fleet";
import { MCP_ROUTE, handleMcpRequest } from "./mcp";

// Compose the bus primitives into the publisher `issueAdminCommand` expects.
// Kept here rather than in fleet.ts so the fleet module stays independent of
// the bus storage adapter and stays unit-testable without one.
function adminPublisher(storage: StorageAdapter): AdminPublisher {
	return (topic, payload, bubbleId) =>
		storage.deliver(createTopicEvent(topic, { source: "fleet", payload }, bubbleId));
}

// How each issue failure surfaces on the REST route. 404/409/503 are the
// statuses this route has always returned; keeping them in a table means the
// MCP surface can map the same outcomes to its own wording without either
// surface inventing a case the other does not handle.
const FLEET_ISSUE_HTTP: Record<IssueFailure, { status: number; body: { error: string } }> = {
	unknown_instance: { status: 404, body: { error: "unknown instance" } },
	not_addressable: { status: 409, body: { error: "instance not addressable (no bubble)" } },
	not_delivered: {
		status: 503,
		body: { error: "no live supervisor admin subscription; command not delivered" },
	},
};

interface Env {
	EVENTS: KVNamespace;
	DEPLOYMENT_SESSION: DurableObjectNamespace;
	INTERNAL_DO_SECRET: string;
	BOBI_RELEASE_VERSION?: string;
	BOBI_RELEASE_SHA?: string;
	CF_VERSION_METADATA?: {
		id?: string;
		tag?: string;
		timestamp?: string;
	};
	WEBHOOK_SECRET?: string;
	SLACK_SIGNING_SECRET?: string;
	LINEAR_WEBHOOK_SECRET?: string;
	WHATSAPP_APP_SECRET?: string;
	WHATSAPP_VERIFY_TOKEN?: string;
	TEST_GRANTS_SECRET?: string;
	// Fleet control-plane API (#6). The operator credential is the epic's
	// "exactly one client to authorize"; the windows tune reachability.
	FLEET_OPERATOR_TOKEN?: string;
	FLEET_LIVE_WINDOW_S?: string;
	FLEET_STALE_WINDOW_S?: string;
	// How long an MCP command tool waits server-side for the supervisor's
	// reply before handing back a command_id to poll. See McpEnv.
	MCP_COMMAND_WAIT_MS?: string;
}

// Topics the sidecar publishes for the fleet read model (#5/#6). Folded into
// KV on a successful signed publish; the names are the producer/consumer
// contract fixed jointly by #5 and #6.
const FLEET_HEARTBEAT_TOPIC = "fleet/heartbeat";
const FLEET_LIFECYCLE_TOPIC = "fleet/lifecycle";

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

		// Ingest tokens (#640). Two keys per token, both holding the (immutable)
		// record: hash-keyed for the hot-path lookup, and a per-bubble composite
		// key for list/revoke. Deliberately NO shared index blob: KV is
		// last-write-wins, and a read-modify-write index loses entries under
		// concurrent mints — a token that authenticates but can never be listed
		// or revoked. Independent puts/deletes cannot race that way.
		async putIngestToken(record: IngestTokenRecord): Promise<void> {
			const json = JSON.stringify(record);
			await Promise.all([
				env.EVENTS.put(`ingest_token:${record.token_hash}`, json),
				env.EVENTS.put(`ingest_token_by_id:${record.bubble_id}:${record.id}`, json),
			]);
		},

		async getIngestTokenByHash(hash: string): Promise<IngestTokenRecord | null> {
			const data = await env.EVENTS.get(`ingest_token:${hash}`);
			return data ? JSON.parse(data) : null;
		},

		async listIngestTokens(bubbleId: string): Promise<IngestTokenRecord[]> {
			// One list page (1000 keys) is far beyond any real per-bubble token
			// count; values fetched in parallel.
			const listed = await env.EVENTS.list({ prefix: `ingest_token_by_id:${bubbleId}:` });
			const values = await Promise.all(listed.keys.map((k) => env.EVENTS.get(k.name)));
			return values.filter((v): v is string => v !== null).map((v) => JSON.parse(v));
		},

		async deleteIngestToken(record: IngestTokenRecord): Promise<void> {
			await Promise.all([
				env.EVENTS.delete(`ingest_token:${record.token_hash}`),
				env.EVENTS.delete(`ingest_token_by_id:${record.bubble_id}:${record.id}`),
			]);
		},

		async getWebhookDelivery(source: string, deliveryKey: string): Promise<WebhookDeliveryRecord | null> {
			const data = await env.EVENTS.get(`webhook_delivery:${source}:${deliveryKey}`);
			return data ? JSON.parse(data) : null;
		},

		async putWebhookDelivery(record: WebhookDeliveryRecord): Promise<void> {
			await env.EVENTS.put(
				`webhook_delivery:${record.source}:${record.delivery_key}`,
				JSON.stringify(record),
				{ expirationTtl: 7 * 24 * 60 * 60 },
			);
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
	async fetch(request: Request, env: Env, ctx?: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);
		const path = url.pathname;
		const method = request.method;
		const storage = createKVStorage(env);

		if (method === "GET" && path === "/health") {
			return Response.json({
				status: "ok",
				auth: "hmac",
				release: {
					version: env.BOBI_RELEASE_VERSION || "dev",
					sha: env.BOBI_RELEASE_SHA || "dev",
				},
				worker: {
					version_id: env.CF_VERSION_METADATA?.id || null,
					version_tag: env.CF_VERSION_METADATA?.tag || null,
					version_timestamp: env.CF_VERSION_METADATA?.timestamp || null,
				},
				rejections: getAuthRejectionCounters(),
			});
		}

			// -----------------------------------------------------------------
			// The agentic control surface: the same fleet control plane served as
			// MCP, so an agent binds to named tools with declared schemas instead
			// of re-deriving the route shapes below. Same credential, same
			// authority - the interface widens, not the authorization. Auth runs
			// here, before dispatch, exactly as it does for /fleet/* below.
			// -----------------------------------------------------------------
			if (path === MCP_ROUTE) {
				const op = requireOperator(request.headers.get("authorization"), env.FLEET_OPERATOR_TOKEN);
				if (op) return op;
				return handleMcpRequest(request, env, adminPublisher(storage), ctx);
			}

			// -----------------------------------------------------------------
			// Fleet control-plane API (#6, #9). Operator-authenticated (one
			// credential), distinct from bus bubble auth. Read routes (Phase A)
			// serve the fleet read model; the command routes (Phase B) publish an
			// admin command onto the target deployment's bubble-namespaced admin
			// topic and fold the supervisor's reply.
			// -----------------------------------------------------------------
			if (path === "/fleet/status" || path.startsWith("/fleet/instances/")) {
				const op = requireOperator(request.headers.get("authorization"), env.FLEET_OPERATOR_TOKEN);
				if (op) return op;
				try {
					const windows = windowsFromEnv(env.FLEET_LIVE_WINDOW_S, env.FLEET_STALE_WINDOW_S);
					const fleetStore = createFleetKVStorage(env.EVENTS);
					const now = Date.now();

					// Percent-decode path segments; a malformed encoding is a 400,
					// not a 500. One helper for all three routes below.
					const decodeSegments = (...segs: string[]): string[] | null => {
						try {
							return segs.map((s) => decodeURIComponent(s));
						} catch {
							return null;
						}
					};

					if (method === "GET" && path === "/fleet/status") {
						return Response.json(await buildFleetStatus(fleetStore, now, windows));
					}

					const instanceMatch = path.match(/^\/fleet\/instances\/([^/]+)\/([^/]+)$/);
					if (method === "GET" && instanceMatch) {
						const decoded = decodeSegments(instanceMatch[1], instanceMatch[2]);
						if (!decoded) return Response.json({ error: "invalid instance path" }, { status: 400 });
						const [fleet, instance] = decoded;
						const detail = await buildInstanceDetail(fleetStore, fleet, instance, now, windows);
						if (!detail) return Response.json({ error: "unknown instance" }, { status: 404 });
						return Response.json(detail);
					}

					// Phase B write half (#9): issue an admin command. The server
					// publishes it INTO the target deployment's bubble on its admin
					// topic (bubble namespacing makes that fail-closed), records it
					// pending, and folds the supervisor's `fleet/command_result`
					// reply, which the GET-by-id below reads back.
					const commandsMatch = path.match(/^\/fleet\/instances\/([^/]+)\/([^/]+)\/commands$/);
					if (method === "POST" && commandsMatch) {
						const decoded = decodeSegments(commandsMatch[1], commandsMatch[2]);
						if (!decoded) return Response.json({ error: "invalid instance path" }, { status: 400 });
						const [fleet, instance] = decoded;
						let body: Record<string, unknown>;
						try {
							body = (await request.json()) as Record<string, unknown>;
						} catch {
							return Response.json({ error: "invalid JSON" }, { status: 400 });
						}
						if (!isAdminCommand(body.command)) {
							return Response.json(
								{ error: "unknown command", allowed: [...ADMIN_COMMANDS] },
								{ status: 400 },
							);
						}
						const args = (body.args && typeof body.args === "object")
							? (body.args as Record<string, unknown>)
							: undefined;
						// Shared with the MCP write tools - one implementation of
						// address-check -> publish -> record, so the two surfaces
						// cannot drift on which commands are issuable or on the
						// deliver-before-record ordering.
						const outcome = await issueAdminCommand(fleetStore, adminPublisher(storage), {
							fleet,
							instance,
							command: body.command,
							args,
							now,
						});
						if (!outcome.ok) {
							return Response.json(FLEET_ISSUE_HTTP[outcome.failure].body, {
								status: FLEET_ISSUE_HTTP[outcome.failure].status,
							});
						}
						return Response.json(
							{
								command_id: outcome.command_id,
								status: "pending",
								delivered_to: outcome.delivered_to,
								status_url: `/fleet/instances/${commandsMatch[1]}/${commandsMatch[2]}/commands/${outcome.command_id}`,
							},
							{ status: 202 },
						);
					}

					const commandIdMatch = path.match(/^\/fleet\/instances\/([^/]+)\/([^/]+)\/commands\/([^/]+)$/);
					if (method === "GET" && commandIdMatch) {
						const decoded = decodeSegments(commandIdMatch[1], commandIdMatch[2], commandIdMatch[3]);
						if (!decoded) return Response.json({ error: "invalid command path" }, { status: 400 });
						const [fleet, instance, commandId] = decoded;
						const view = await buildCommandView(fleetStore, fleet, instance, commandId);
						if (!view) return Response.json({ error: "unknown command" }, { status: 404 });
						return Response.json(view);
					}

					return Response.json({ error: "not found" }, { status: 404 });
				} catch (e) {
					// A corrupt read-model value must not surface as an unhandled
					// throw; the list readers already skip bad values, this is the
					// backstop.
					console.error("fleet query failed", path, e);
					return Response.json({ error: "internal error" }, { status: 500 });
				}
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
			const handshakeRoute = matchWebhookSource(path);
			if (handshakeRoute) {
				const h = handleWebhookHandshake(
					handshakeRoute.source, (n) => url.searchParams.get(n) || "", webhookSecrets);
				if (h) {
					return new Response(h.text, {
						status: h.status,
						headers: { "Content-Type": "text/plain" },
					});
				}
			}
		}

		const webhookRoute = method === "POST" ? matchWebhookSource(path) : null;
		if (webhookRoute) {
			const rawBody = await request.text();
			const waitUntil = typeof ctx?.waitUntil === "function" ? ctx.waitUntil.bind(ctx) : undefined;
			const result = await handleWebhookRequest(
				storage,
				webhookRoute.source,
				{
					rawBody,
					header: (n) => request.headers.get(n) || "",
					subpath: webhookRoute.subpath,
				},
				webhookSecrets,
				waitUntil ? { waitUntil } : {},
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
			const topic = topicMatch[1];
			const result = await handleTopicEvent(storage, topic, data, ctx);
			// Fold the sidecar's fleet telemetry into the control-plane read model
			// (#6, #9). Only on a SUCCESSFUL publish (handleTopicEvent
			// authenticated the bubble signature), so an unsigned/forged POST
			// never folds - and because it succeeded, ctx.bubbleId is the VERIFIED
			// publisher, captured on the heartbeat so the command route can reach
			// this deployment's admin topic in its own bubble. The fold is
			// best-effort: a fold error must not fail the publish.
			if (
				result.status < 300 &&
				(topic === FLEET_HEARTBEAT_TOPIC ||
					topic === FLEET_LIFECYCLE_TOPIC ||
					topic === COMMAND_RESULT_TOPIC)
			) {
				const payload = (data.payload ?? {}) as Record<string, unknown>;
				const fleetStore = createFleetKVStorage(env.EVENTS);
				const receivedAt = Date.now();
				try {
					if (topic === FLEET_HEARTBEAT_TOPIC) {
						await foldHeartbeat(fleetStore, payload as unknown as FleetSnapshot, receivedAt, ctx.bubbleId);
					} else if (topic === FLEET_LIFECYCLE_TOPIC) {
						await foldLifecycle(fleetStore, payload as unknown as LifecyclePayload, receivedAt);
					} else {
						await foldCommandResult(fleetStore, payload as unknown as CommandResultPayload, receivedAt);
					}
				} catch (e) {
					console.error("fleet fold failed", topic, e);
				}
			}
			return respond(result);
		}

		if (method === "POST" && path === "/channels/send") {
			// Channel-agnostic send (#618) - mandatory bubble auth.
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
			// Auth is MANDATORY (no legacy unsigned caller).
			// The credential in the body is verified once and never persisted; the
			// route is deliberately excluded from any body logging.
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleAuthorizeResource(storage, auth.data, auth.bubble.id));
		}

		// Scoped ingest tokens (#640) — mint/list/revoke, mandatory bubble auth
		// (mirrors /resources/authorize). The mint response is the only place
		// the plaintext token ever appears.
		if (method === "POST" && path === "/ingest-tokens") {
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleIngestTokenCreate(storage, auth.data, auth.bubble.id));
		}

		if (method === "GET" && path === "/ingest-tokens") {
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleIngestTokenList(storage, auth.bubble.id));
		}

		const ingestTokenMatch = method === "DELETE" && path.match(/^\/ingest-tokens\/([^/]+)$/);
		if (ingestTokenMatch) {
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleIngestTokenRevoke(storage, ingestTokenMatch[1], auth.bubble.id));
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
			// outbound channel sends read. A present-but-invalid signature is a
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
			// use case, so an unauthenticated registration is rejected outright
			// (bubbleAuthedJson fails closed on unsigned requests).
			const auth = await bubbleAuthedJson(request, url, storage);
			if (auth instanceof Response) return auth;
			return respond(await handleWhatsAppNumberRegister(storage, auth.data, auth.bubble.id));
		}

		return new Response("Not Found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;
