import http from "node:http";
import type { Duplex } from "node:stream";
import { WebSocketServer, type WebSocket } from "ws";
import {
	type NormalizedEvent,
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type SlackWorkspaceRecord,
	type ResourceGrant,
	type IngestTokenRecord,
	type WebhookDeliveryRecord,
	type HandlerResult,
	subscriptionKeysForEvent,
	namespaceSubKey,
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
	handleDiscordAppRegister,
	handleTestSeedResourceGrants,
	envIngestTokenRecords,
	getAuthRejectionCounters,
} from "@moda-labs/bobi-events-core";
import { DiscordGatewayManager } from "./discord-gateway-local";
import {
	SlackSocketManager,
	slackSocketConfigurationError,
} from "./slack-socket-local";
import {
	isExemptFromBreaker,
	recordDelivery,
	drainPaused,
	conversationKey,
	buildLoopDetectedEvent,
} from "@moda-labs/bobi-events-core/circuit-breaker";
import { setSlackApiUrl, setWhatsAppApiUrl, setDiscordApiUrl } from "@moda-labs/bobi-events-core/channels";

// Integration-test seams: point the Slack Web API / Meta Graph API / Discord
// API at local stubs. Unset in production, where the platform defaults apply.
const slackApiUrlOverride = process.env.BOBI_ES_SLACK_API_URL || "";
setSlackApiUrl(slackApiUrlOverride);
setWhatsAppApiUrl(process.env.BOBI_ES_WHATSAPP_API_URL);
setDiscordApiUrl(process.env.BOBI_ES_DISCORD_API_URL);

const port = parseInt(process.env.BOBI_ES_PORT || "8080", 10);
const bind = process.env.BOBI_ES_BIND || "127.0.0.1";
const slackSocketConfigError = slackSocketConfigurationError(bind, slackApiUrlOverride);

const MAX_BUFFER = 10_000;

// Eviction backstop (#279): a deployment with zero WebSocket connections
// for longer than this threshold is considered leaked (client SIGKILLed
// before calling DELETE) and will be removed.  Keyed on WS-disconnect,
// NOT activity — a live manager session can idle for hours with zero
// events but its WS stays connected, so activity-based TTL would break
// inbox delivery.
const EVICTION_STALE_MS = parseInt(process.env.BOBI_ES_EVICTION_STALE_MS || "60000", 10);
const EVICTION_SWEEP_MS = parseInt(process.env.BOBI_ES_EVICTION_SWEEP_MS || "30000", 10);

// ---------------------------------------------------------------------------
// In-memory state — extends DeploymentRecord with runtime fields
// ---------------------------------------------------------------------------

interface LocalDeployment {
	id: string;
	name: string;
	apiKey: string;
	bubbleId: string;
	subscriptions: string[];
	nextSeq: number;
	eventBuffer: Array<NormalizedEvent & { seq: number }>;
	websockets: Set<WebSocket>;
	// Timestamp (ms) when the last WebSocket disconnected, or null if at
	// least one WS is still connected.  Used by the eviction sweep to
	// remove deployments leaked by a crashed client (SIGKILL before DELETE).
	disconnectedAt: number | null;
}

const deployments = new Map<string, LocalDeployment>();
const apiKeyIndex = new Map<string, string>();
const subscriptionIndex = new Map<string, Set<string>>();
const bubbles = new Map<string, BubbleRecord>();
const slackWorkspaces = new Map<string, SlackWorkspaceRecord>();
const channelState = new Map<string, Record<string, unknown>>();
// Resource grants (#488), keyed `service:resource:bubbleId`. In-memory and
// strongly consistent, so the Worker's KV-propagation race never applies here.
const resourceGrants = new Map<string, ResourceGrant>();
// Scoped ingest tokens (#640), keyed by token HASH (the only lookup the hot
// path needs); list/revoke scan per bubble — token counts are tiny.
const ingestTokens = new Map<string, IngestTokenRecord>();
const webhookDeliveries = new Map<string, WebhookDeliveryRecord>();
const ENV_PENDING_BUBBLE_ID = "__env_ingest_tokens_pending__";
let envIngestTokensAttached = false;

const webhookSecret = process.env.BOBI_ES_WEBHOOK_SECRET || "";
const slackSigningSecret = process.env.BOBI_ES_SLACK_SIGNING_SECRET || "";
const linearWebhookSecret = process.env.BOBI_ES_LINEAR_WEBHOOK_SECRET || "";
const whatsappAppSecret = process.env.BOBI_ES_WHATSAPP_APP_SECRET || "";
const whatsappVerifyToken = process.env.BOBI_ES_WHATSAPP_VERIFY_TOKEN || "";

const webhookSecrets = {
	github: webhookSecret,
	slack: slackSigningSecret,
	linear: linearWebhookSecret,
	whatsapp: whatsappAppSecret,
	whatsappVerifyToken,
};
const testGrantsSecret = process.env.BOBI_ES_TEST_GRANTS_SECRET || "";
const releaseVersion = process.env.BOBI_RELEASE_VERSION || "local";
const releaseSha = process.env.BOBI_RELEASE_SHA || "local";

// Discord Gateway connection manager (inbound rides a persistent outbound
// WebSocket, not a webhook). Declared here, constructed after the storage
// adapter below; connections start from env config in main() and from
// POST /discord/apps registrations.
const discordBotToken = process.env.BOBI_ES_DISCORD_BOT_TOKEN || "";
const discordApplicationId = process.env.BOBI_ES_DISCORD_APPLICATION_ID || "";
const discordMessageContent = process.env.BOBI_ES_DISCORD_MESSAGE_CONTENT === "1";
const discordGatewayUrl = process.env.BOBI_ES_DISCORD_GATEWAY_URL || "";

// ---------------------------------------------------------------------------
// Map-based storage adapter
// ---------------------------------------------------------------------------

const storage: StorageAdapter = {
	async getDeploymentByApiKey(apiKey: string): Promise<DeploymentRecord | null> {
		const id = apiKeyIndex.get(apiKey);
		if (!id) return null;
		const dep = deployments.get(id);
		if (!dep) return null;
		return {
			id: dep.id,
			name: dep.name,
			api_key: dep.apiKey,
			bubble_id: dep.bubbleId,
			subscriptions: [...dep.subscriptions],
		};
	},

	async getDeploymentByName(name: string, bubbleId: string): Promise<DeploymentRecord | null> {
		for (const dep of deployments.values()) {
			if (dep.name === name && dep.bubbleId === bubbleId) {
				return {
					id: dep.id,
					name: dep.name,
					api_key: dep.apiKey,
					bubble_id: dep.bubbleId,
					subscriptions: [...dep.subscriptions],
				};
			}
		}
		return null;
	},

	async getDeploymentById(id: string): Promise<DeploymentRecord | null> {
		const dep = deployments.get(id);
		if (!dep) return null;
		return {
			id: dep.id,
			name: dep.name,
			api_key: dep.apiKey,
			bubble_id: dep.bubbleId,
			subscriptions: [...dep.subscriptions],
		};
	},

	async putResourceGrant(grant: ResourceGrant): Promise<void> {
		resourceGrants.set(`${grant.service}:${grant.resource}:${grant.bubble_id}`, grant);
	},

	async hasResourceGrant(service: string, resource: string, bubbleId: string): Promise<boolean> {
		return resourceGrants.has(`${service}:${resource}:${bubbleId}`);
	},

	async putIngestToken(record: IngestTokenRecord): Promise<void> {
		ingestTokens.set(record.token_hash, record);
	},

	async getIngestTokenByHash(hash: string): Promise<IngestTokenRecord | null> {
		const record = ingestTokens.get(hash) || null;
		if (record?.bubble_id === ENV_PENDING_BUBBLE_ID) return null;
		return record;
	},

	async listIngestTokens(bubbleId: string): Promise<IngestTokenRecord[]> {
		return [...ingestTokens.values()].filter((r) => r.bubble_id === bubbleId);
	},

	async deleteIngestToken(record: IngestTokenRecord): Promise<void> {
		ingestTokens.delete(record.token_hash);
	},

	async getWebhookDelivery(source: string, deliveryKey: string): Promise<WebhookDeliveryRecord | null> {
		return webhookDeliveries.get(`${source}:${deliveryKey}`) || null;
	},

	async putWebhookDelivery(record: WebhookDeliveryRecord): Promise<void> {
		webhookDeliveries.set(`${record.source}:${record.delivery_key}`, record);
	},

	async putDeployment(record: DeploymentRecord): Promise<void> {
		const existing = deployments.get(record.id);
		if (existing) {
			existing.name = record.name;
			existing.bubbleId = record.bubble_id;
			existing.subscriptions = [...record.subscriptions];
		} else {
			deployments.set(record.id, {
				id: record.id,
				name: record.name,
				apiKey: record.api_key,
				bubbleId: record.bubble_id,
				subscriptions: [...record.subscriptions],
				nextSeq: 1,
				eventBuffer: [],
				websockets: new Set(),
				disconnectedAt: Date.now(),
			});
			apiKeyIndex.set(record.api_key, record.id);
		}
	},

	async getBubble(bubbleId: string): Promise<BubbleRecord | null> {
		return bubbles.get(bubbleId) || null;
	},

	async putBubble(bubble: BubbleRecord): Promise<void> {
		bubbles.set(bubble.id, bubble);
	},

	async removeDeployment(record: DeploymentRecord): Promise<void> {
		const dep = deployments.get(record.id);
		if (dep) {
			for (const ws of dep.websockets) {
				try { ws.close(); } catch { /* best-effort */ }
			}
		}
		deployments.delete(record.id);
		apiKeyIndex.delete(record.api_key);
	},

	async addSubscription(key: string, deploymentId: string): Promise<void> {
		if (!subscriptionIndex.has(key)) subscriptionIndex.set(key, new Set());
		subscriptionIndex.get(key)!.add(deploymentId);
	},

	async removeSubscription(key: string, deploymentId: string): Promise<void> {
		const set = subscriptionIndex.get(key);
		if (set) {
			set.delete(deploymentId);
			if (set.size === 0) subscriptionIndex.delete(key);
		}
	},

	async deliver(event: NormalizedEvent): Promise<number> {
		// Enforcement layer 2 (#488): grant-filter GLOBAL topics at delivery so a
		// stale subscription-index entry for an un-granted bubble is dropped here
		// (the authoritative, fail-closed boundary).
		const depIds = await admittedDeploymentIds(
			storage,
			event,
			async (key) => subscriptionIndex.get(key) ?? [],
		);

		const exempt = isExemptFromBreaker(event);

		let delivered = 0;
		for (const depId of depIds) {
			const dep = deployments.get(depId);
			if (!dep) continue;

			// Circuit breaker: check before delivering (exempt events bypass)
			if (!exempt) {
				const verdict = recordDelivery(depId, event);
				if (verdict.justTripped) {
					// Emit system.loop_detected — deliver to all subscribers
					const convKey = conversationKey(event)!;
					const loopEvent = buildLoopDetectedEvent(depId, convKey, event);
					const loopKeys = subscriptionKeysForEvent(loopEvent);
					for (const lk of loopKeys) {
						for (const lid of subscriptionIndex.get(lk) || []) {
							const ldep = deployments.get(lid);
							if (!ldep) continue;
							const lseq = ldep.nextSeq++;
							const lseqEvent = { ...loopEvent, seq: lseq };
							ldep.eventBuffer.push(lseqEvent);
							if (ldep.eventBuffer.length >= 2 * MAX_BUFFER) {
								ldep.eventBuffer = ldep.eventBuffer.slice(-MAX_BUFFER);
							}
							const lmsg = JSON.stringify({ type: "event", data: lseqEvent });
							for (const ws of ldep.websockets) {
								try { ws.send(lmsg); } catch { ldep.websockets.delete(ws); }
							}
						}
					}
				}
				if (!verdict.allow) continue;

				// Human event may have unpaused — drain buffered events
				const drained = drainPaused(depId, event);
				for (const paused of drained) {
					const pseq = dep.nextSeq++;
					const pseqEvent = { ...paused, seq: pseq };
					dep.eventBuffer.push(pseqEvent);
					if (dep.eventBuffer.length >= 2 * MAX_BUFFER) {
						dep.eventBuffer = dep.eventBuffer.slice(-MAX_BUFFER);
					}
					const pmsg = JSON.stringify({ type: "event", data: pseqEvent });
					for (const ws of dep.websockets) {
						try { ws.send(pmsg); } catch { dep.websockets.delete(ws); }
					}
				}
			}

			const seq = dep.nextSeq++;
			const seqEvent = { ...event, seq };
			dep.eventBuffer.push(seqEvent);
			if (dep.eventBuffer.length >= 2 * MAX_BUFFER) {
				dep.eventBuffer = dep.eventBuffer.slice(-MAX_BUFFER);
			}
			delivered++;

			const msg = JSON.stringify({ type: "event", data: seqEvent });
			for (const ws of dep.websockets) {
				try {
					ws.send(msg);
				} catch {
					dep.websockets.delete(ws);
				}
			}
		}

		return delivered;
	},

	async getSlackWorkspace(workspaceId: string): Promise<SlackWorkspaceRecord | null> {
		return slackWorkspaces.get(workspaceId) || null;
	},

	async putSlackWorkspace(workspaceId: string, record: SlackWorkspaceRecord): Promise<void> {
		slackWorkspaces.set(workspaceId, record);
	},

	async getChannelState(key: string): Promise<Record<string, unknown> | null> {
		return channelState.get(key) || null;
	},

	async putChannelState(key: string, value: Record<string, unknown>): Promise<void> {
		channelState.set(key, value);
	},

	async initDeploymentSession(deploymentId: string): Promise<void> {
		// Deployment subscriptions have been indexed by now. Attach env-managed
		// ingest tokens lazily to the first real local bubble so restarts preserve
		// sender credentials without persisting plaintext.
		if (!envIngestTokensAttached) {
			const dep = deployments.get(deploymentId);
			if (dep) attachEnvIngestTokens(dep.bubbleId);
		}
	},
};

const discordGateway = new DiscordGatewayManager(storage, {
	messageContent: discordMessageContent,
	...(discordGatewayUrl ? { gatewayUrlOverride: discordGatewayUrl } : {}),
});
const slackSocket = new SlackSocketManager(storage, {
	bindAddress: bind,
	apiUrlOverride: slackApiUrlOverride,
});

async function seedEnvIngestTokens() {
	const configured = process.env.BOBI_ES_INGEST_TOKENS || "";
	if (!configured.trim()) return;
	const records = await envIngestTokenRecords(configured, ENV_PENDING_BUBBLE_ID);
	for (const record of records) {
		await storage.putIngestToken(record);
	}
	console.log(`seeded ${records.length} env-managed ingest token(s) from BOBI_ES_INGEST_TOKENS`);
}

function attachEnvIngestTokens(bubbleId: string) {
	let attached = 0;
	for (const record of ingestTokens.values()) {
		if (record.env_managed && record.bubble_id === ENV_PENDING_BUBBLE_ID) {
			record.bubble_id = bubbleId;
			attached++;
		}
	}
	if (attached > 0) envIngestTokensAttached = true;
}

// ---------------------------------------------------------------------------
// Node.js HTTP helpers
// ---------------------------------------------------------------------------

function readBody(req: http.IncomingMessage): Promise<string> {
	return new Promise((resolve, reject) => {
		const chunks: Buffer[] = [];
		req.on("data", (chunk: Buffer) => chunks.push(chunk));
		req.on("end", () => resolve(Buffer.concat(chunks).toString()));
		req.on("error", reject);
	});
}

function json(res: http.ServerResponse, data: unknown, status = 200) {
	res.writeHead(status, { "Content-Type": "application/json" });
	res.end(JSON.stringify(data));
}

function parseJson(body: string): Record<string, unknown> | null {
	try {
		return JSON.parse(body);
	} catch {
		return null;
	}
}

function respond(res: http.ServerResponse, result: HandlerResult) {
	json(res, result.body, result.status);
}

// Shared prologue of every mandatory bubble-signed route: read the exact wire
// bytes (the signature covers them), parse JSON, authenticate. Writes the
// error response and returns null on failure. A GET carries an empty body,
// which verifies and parses as {}.
async function bubbleAuthedJson(
	req: http.IncomingMessage,
	res: http.ServerResponse,
	url: URL,
): Promise<{ bubble: BubbleRecord; data: Record<string, unknown> } | null> {
	const body = await readBody(req);
	let data: Record<string, unknown> = {};
	if (body) {
		const parsed = parseJson(body);
		if (!parsed) {
			json(res, { error: "invalid JSON" }, 400);
			return null;
		}
		data = parsed;
	}
	const ctx = readBubbleAuthHeaders(
		(n) => req.headers[n] as string | undefined,
		req.method || "GET",
		url.pathname + url.search,
		body,
	);
	const bubble = await authenticateBubble(storage, ctx);
	if (!bubble) {
		json(res, { error: "forbidden" }, 403);
		return null;
	}
	return { bubble, data };
}

// ---------------------------------------------------------------------------
// Routing table
// ---------------------------------------------------------------------------

async function handleRequest(req: http.IncomingMessage, res: http.ServerResponse) {
	const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
	const path = url.pathname;
	const method = req.method || "GET";

	if (method === "GET" && path === "/health") {
		const discordHealth = discordGateway.health();
		const slackHealth = slackSocket.health();
		return json(res, {
			status: "ok",
			mode: "local",
			deployments: deployments.size,
			auth: "hmac",
			release: {
				version: releaseVersion,
				sha: releaseSha,
			},
			bubbles: bubbles.size,
			rejections: getAuthRejectionCounters(),
			...(discordHealth.length > 0 ? { discord_gateway: discordHealth } : {}),
			...(slackHealth.length > 0 ? { slack_socket: slackHealth } : {}),
		});
	}

	// Inbound webhooks — one pipeline for every source (#639), shared with the
	// Worker: the core matches the route, verifies the exact wire bytes (the
	// verify slot is structural), normalizes, and delivers. matchWebhookSource
	// returns null for an unregistered path, which 404s below WITHOUT
	// consuming the request body.
	// Provider GET handshakes (#656): Meta verifies a webhook URL with a GET
	// whose challenge must echo back as RAW text (JSON-quoting breaks its
	// byte comparison), so this responds outside respond()/JSON.
	if (method === "GET") {
		const handshakeRoute = matchWebhookSource(path);
		if (handshakeRoute) {
			const h = handleWebhookHandshake(
				handshakeRoute.source, (n) => url.searchParams.get(n) || "", webhookSecrets);
			if (h) {
				res.writeHead(h.status, { "Content-Type": "text/plain" });
				res.end(h.text);
				return;
			}
		}
	}

	const webhookRoute = method === "POST" ? matchWebhookSource(path) : null;
	if (webhookRoute) {
		const body = await readBody(req);
		const result = await handleWebhookRequest(
			storage,
			webhookRoute.source,
			{
				rawBody: body,
				header: (n) => (req.headers[n.toLowerCase()] as string) || "",
				subpath: webhookRoute.subpath,
			},
			webhookSecrets,
		);
		if (result) return respond(res, result);
	}

	if (method === "POST" && path === "/deployments") {
		const body = await readBody(req);
		const data = parseJson(body);
		if (!data) return json(res, { error: "invalid JSON" }, 400);
		const ctx = readBubbleAuthHeaders(
			(n) => req.headers[n] as string | undefined,
			method,
			url.pathname + url.search,
			body,
		);
		return respond(res, await handleRegisterDeployment(storage, data, ctx));
	}

	const subsMatch = path.match(/^\/deployments\/([^/]+)\/subscriptions$/);
	if (method === "PUT" && subsMatch) {
		const authHeader = req.headers.authorization || "";
		if (!authHeader.startsWith("Bearer ")) return json(res, { error: "unauthorized" }, 403);
		const apiKey = authHeader.slice(7);

		const body = await readBody(req);
		const data = parseJson(body);
		if (!data) return json(res, { error: "invalid JSON" }, 400);

		return respond(res, await handleUpdateSubscriptions(storage, subsMatch[1], apiKey, data));
	}

	const deleteMatch = path.match(/^\/deployments\/([^/]+)$/);
	if (method === "DELETE" && deleteMatch) {
		const authHeader = req.headers.authorization || "";
		if (!authHeader.startsWith("Bearer ")) return json(res, { error: "unauthorized" }, 403);
		const apiKey = authHeader.slice(7);
		return respond(res, await handleDeregisterDeployment(storage, deleteMatch[1], apiKey));
	}

	// Generic topic: POST /events/{topic}
	const topicMatch = method === "POST" && path.match(/^\/events\/(.+)$/);
	if (topicMatch) {
		const body = await readBody(req);
		const data = parseJson(body);
		if (!data) return json(res, { error: "invalid JSON" }, 400);
		const ctx = readBubbleAuthHeaders(
			(n) => req.headers[n] as string | undefined,
			method,
			url.pathname + url.search,
			body,
		);
		return respond(res, await handleTopicEvent(storage, topicMatch[1], data, ctx));
	}

	if (method === "POST" && path === "/slack/workspaces") {
		const body = await readBody(req);
		const data = parseJson(body);
		if (!data) return json(res, { error: "invalid JSON" }, 400);
		const ctx = readBubbleAuthHeaders(
			(n) => req.headers[n] as string | undefined,
			method,
			url.pathname + url.search,
			body,
		);
		// Auth is OPTIONAL: an unsigned registration still writes the global
		// record (inbound self-reply loop prevention, kept for legacy clients);
		// a signed one ALSO writes the bubble-scoped record outbound send reads.
		// A present-but-invalid signature is rejected, never silently downgraded.
		let bubbleId: string | undefined;
		if (hasBubbleSignature(ctx)) {
			const bubble = await authenticateBubble(storage, ctx);
			if (!bubble) return json(res, { error: "forbidden" }, 403);
			bubbleId = bubble.id;
		} else if (hasPartialBubbleSignature(ctx)) {
			return json(res, { error: "forbidden" }, 403);
		}
		if (bubbleId && typeof data.app_token === "string"
			&& data.app_token.trim().length > 0 && slackSocketConfigError) {
			return json(res, { error: slackSocketConfigError }, 503);
		}
		const result = await handleSlackWorkspaceRegister(storage, data, bubbleId);
		if (result.status === 200 && bubbleId && typeof data.app_token === "string") {
			const response = result.body as Record<string, unknown>;
			const workspaceId = typeof response.workspace_id === "string"
				? response.workspace_id
				: "";
			const applicationId = typeof response.app_id === "string"
				? response.app_id
				: "";
			const botId = typeof response.bot_id === "string" ? response.bot_id : "";
			slackSocket.start({
				registrationId: `${workspaceId}:${botId || "default"}`,
				appToken: data.app_token,
				...(applicationId ? { applicationId } : {}),
			});
		}
		return respond(res, result);
	}

	if (method === "POST" && path === "/whatsapp/numbers") {
		// Signed-only (#656): no unsigned global-record use case exists, so
		// the mandatory bubble-auth prologue applies.
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleWhatsAppNumberRegister(storage, auth.data, auth.bubble.id));
	}

	if (method === "POST" && path === "/discord/apps") {
		// Signed-only, mirroring /whatsapp/numbers. A successful registration
		// also starts (or refreshes) the app's Gateway connection so inbound
		// messages flow without a restart.
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		const result = await handleDiscordAppRegister(storage, auth.data, auth.bubble.id);
		if (result.status === 200) {
			discordGateway.start(
				auth.data.application_id as string,
				auth.data.bot_token as string,
			);
		}
		return respond(res, result);
	}

	if (method === "POST" && path === "/channels/send") {
		// Channel-agnostic send (#618) - mandatory bubble auth.
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleChannelsSend(storage, auth.data, auth.bubble.id));
	}

	if (method === "POST" && path === "/channels/typing") {
		// Set/clear a channel's thinking indicator (#629).
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleChannelsTyping(storage, auth.data, auth.bubble.id));
	}

	if (method === "GET" && path === "/channels/history") {
		// Read a conversation's messages (#629). The signature covers the full
		// path INCLUDING the query string, with an empty body.
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		const conversation = url.searchParams.get("conversation") || "";
		const limit = parseInt(url.searchParams.get("limit") || "100", 10);
		return respond(res, await handleChannelsHistory(storage, conversation, limit, auth.bubble.id));
	}

	if (method === "POST" && path === "/resources/authorize") {
		// Mandatory bubble auth; the credential in the body is verified once and
		// never persisted/logged.
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleAuthorizeResource(storage, auth.data, auth.bubble.id));
	}

	// Scoped ingest tokens (#640) — mint/list/revoke, mandatory bubble auth
	// (mirrors /resources/authorize). The mint response is the only place the
	// plaintext token ever appears.
	if (method === "POST" && path === "/ingest-tokens") {
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleIngestTokenCreate(storage, auth.data, auth.bubble.id));
	}

	if (method === "GET" && path === "/ingest-tokens") {
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleIngestTokenList(storage, auth.bubble.id));
	}

	const ingestTokenMatch = path.match(/^\/ingest-tokens\/([^/]+)$/);
	if (method === "DELETE" && ingestTokenMatch) {
		const auth = await bubbleAuthedJson(req, res, url);
		if (!auth) return;
		return respond(res, await handleIngestTokenRevoke(storage, ingestTokenMatch[1], auth.bubble.id));
	}

	if (method === "POST" && path === "/__test/resource-grants" && testGrantsSecret) {
		if (req.headers["x-moda-test-secret"] !== testGrantsSecret) {
			return json(res, { error: "not found" }, 404);
		}
		const body = await readBody(req);
		const data = parseJson(body);
		if (!data) return json(res, { error: "invalid JSON" }, 400);
		const ctx = readBubbleAuthHeaders(
			(n) => req.headers[n] as string | undefined,
			method,
			url.pathname + url.search,
			body,
		);
		const bubble = await authenticateBubble(storage, ctx);
		if (!bubble) return json(res, { error: "not found" }, 404);
		return respond(res, await handleTestSeedResourceGrants(storage, data, bubble.id));
	}

	res.writeHead(404);
	res.end("Not Found");
}

// ---------------------------------------------------------------------------
// WebSocket upgrade — transport-specific (node ws)
// ---------------------------------------------------------------------------

function handleUpgrade(req: http.IncomingMessage, socket: Duplex, head: Buffer, wss: WebSocketServer) {
	const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
	const match = url.pathname.match(/^\/deployments\/([^/]+)\/subscribe$/);
	if (!match) {
		socket.destroy();
		return;
	}

	const deploymentId = match[1];
	// Header-only — the `?token=` query fallback was a credential-in-URL leak
	// (logged in access logs). The deployment's subscriptions are already
	// bubble-namespaced at registration, so authenticating the socket as this
	// deployment is sufficient for read isolation.
	const auth = req.headers.authorization || "";
	const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";

	if (!token) {
		socket.destroy();
		return;
	}

	const depId = apiKeyIndex.get(token);
	if (!depId || depId !== deploymentId) {
		socket.destroy();
		return;
	}

	const dep = deployments.get(depId);
	if (!dep) {
		socket.destroy();
		return;
	}

	wss.handleUpgrade(req, socket, head, (ws) => {
		// Close stale WebSockets before accepting the new one (#322).
		// WebSocket reconnections are routine (Cloudflare cycling, process
		// restarts, session rotation) — not just network blips. The old
		// socket lingers in dep.websockets until TCP detects the failure;
		// during that window deliver() sends every event to BOTH sockets,
		// producing duplicate "Evaluating…" placeholders in Slack.
		for (const old of dep.websockets) {
			try { old.close(1000, "replaced"); } catch { /* already closed */ }
		}
		dep.websockets.clear();

		const requestedLastSeen = parseInt(
			url.searchParams.get("last_seen") || "0",
			10,
		);
		const lastSeen =
			Number.isSafeInteger(requestedLastSeen) && requestedLastSeen >= 0
				? requestedLastSeen
				: 0;
		// Zero is a real cursor: the client has processed nothing yet. Skipping
		// replay at zero silently lost an unacked first event (seq=1) whenever a
		// manager restarted before finishing it (#799).
		for (const stored of dep.eventBuffer) {
			if (stored.seq > lastSeen) {
				try {
					ws.send(JSON.stringify({ type: "replay", data: stored }));
				} catch {
					break;
				}
			}
		}

		ws.send(
			JSON.stringify({
				type: "connected",
				deployment_id: deploymentId,
				next_seq: dep.nextSeq,
			}),
		);

		dep.websockets.add(ws);
		dep.disconnectedAt = null; // at least one WS connected

		ws.on("message", (raw) => {
			try {
				const msg = JSON.parse(raw.toString());
				if (msg.type === "ping") {
					ws.send(JSON.stringify({ type: "pong" }));
				}
			} catch {
				// Ignore
			}
		});

		const onDisconnect = () => {
			dep.websockets.delete(ws);
			if (dep.websockets.size === 0) {
				dep.disconnectedAt = Date.now();
			}
		};
		ws.on("close", onDisconnect);
		ws.on("error", onDisconnect);
	});
}


// ---------------------------------------------------------------------------
// Eviction backstop (#279) — periodic sweep for leaked deployments
// ---------------------------------------------------------------------------

function evictStaleDeployments() {
	const now = Date.now();
	for (const [id, dep] of deployments) {
		if (dep.disconnectedAt !== null && now - dep.disconnectedAt >= EVICTION_STALE_MS) {
			// Remove subscription-index entries
			for (const sub of dep.subscriptions) {
				const nsKey = namespaceSubKey(dep.bubbleId, sub);
				const set = subscriptionIndex.get(nsKey);
				if (set) {
					set.delete(id);
					if (set.size === 0) subscriptionIndex.delete(nsKey);
				}
			}
			deployments.delete(id);
			apiKeyIndex.delete(dep.apiKey);
			console.log(`evicted stale deployment ${id} (${dep.name}) — disconnected ${Math.round((now - dep.disconnectedAt) / 1000)}s ago`);
		}
	}
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const server = http.createServer(async (req, res) => {
	try {
		await handleRequest(req, res);
	} catch (err) {
		console.error("Request error:", err);
		if (!res.headersSent) {
			res.writeHead(500);
			res.end("Internal Server Error");
		}
	}
});

const wss = new WebSocketServer({ noServer: true });
server.on("upgrade", (req, socket, head) => handleUpgrade(req, socket, head, wss));

// Start eviction sweep — unref() so it does not keep the process alive
const evictionTimer = setInterval(evictStaleDeployments, EVICTION_SWEEP_MS);
evictionTimer.unref();

async function main() {
	await seedEnvIngestTokens();
	// Single-tenant local dev: an env-configured bot connects at boot, before
	// any signed registration. Registered apps start on POST /discord/apps.
	if (discordBotToken && discordApplicationId) {
		discordGateway.start(discordApplicationId, discordBotToken);
		console.log(`discord gateway: starting connection for env-configured app ${discordApplicationId}`);
	}
	server.listen(port, bind, () => {
		if (bind === "127.0.0.1" || bind === "::1") {
			console.log(
				`bobi event server (local) listening on ${bind}:${port} ` +
					`(loopback-only; set BOBI_ES_BIND to serve remotely)`,
			);
		} else {
			console.log(`bobi event server (local) listening on ${bind}:${port}`);
		}
	});
}

main().catch((err) => {
	console.error("Failed to start local event server:", err);
	process.exit(1);
});
