import http from "node:http";
import { WebSocketServer, type WebSocket } from "ws";
import {
	type NormalizedEvent,
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type SlackWorkspaceRecord,
	type HandlerResult,
	subscriptionKeysForEvent,
	namespaceSubKey,
	verifyGitHubSignature,
	verifySlackSignature,
	readBubbleAuthHeaders,
	hasBubbleSignature,
	hasPartialBubbleSignature,
	authenticateBubble,
	handleGitHubWebhook,
	handleLinearWebhook,
	handleSlackWebhook,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleDeregisterDeployment,
	handleTopicEvent,
	handleSlackSend,
	handleSlackWorkspaceRegister,
	slackSigningSecretFor,
	getAuthRejectionCounters,
} from "./core";
import {
	isExemptFromBreaker,
	recordDelivery,
	drainPaused,
	conversationKey,
	buildLoopDetectedEvent,
} from "./circuit-breaker";

const MAX_BUFFER = 10_000;

// Eviction backstop (#279): a deployment with zero WebSocket connections
// for longer than this threshold is considered leaked (client SIGKILLed
// before calling DELETE) and will be removed.  Keyed on WS-disconnect,
// NOT activity — a live manager session can idle for hours with zero
// events but its WS stays connected, so activity-based TTL would break
// inbox delivery.
const EVICTION_STALE_MS = parseInt(process.env.MODASTACK_ES_EVICTION_STALE_MS || "60000", 10);
const EVICTION_SWEEP_MS = parseInt(process.env.MODASTACK_ES_EVICTION_SWEEP_MS || "30000", 10);

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

const webhookSecret = process.env.MODASTACK_ES_WEBHOOK_SECRET || "";
const slackSigningSecret = process.env.MODASTACK_ES_SLACK_SIGNING_SECRET || "";

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
		const keys = subscriptionKeysForEvent(event);
		const depIds = new Set<string>();
		for (const key of keys) {
			for (const id of subscriptionIndex.get(key) || []) {
				depIds.add(id);
			}
		}

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

	async initDeploymentSession(): Promise<void> {
		// no-op for local — deployment is fully initialized in putDeployment
	},
};

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

// ---------------------------------------------------------------------------
// Routing table
// ---------------------------------------------------------------------------

async function handleRequest(req: http.IncomingMessage, res: http.ServerResponse) {
	const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
	const path = url.pathname;
	const method = req.method || "GET";

	if (method === "GET" && path === "/health") {
		return json(res, {
			status: "ok",
			mode: "local",
			deployments: deployments.size,
			auth: "hmac",
			bubbles: bubbles.size,
			rejections: getAuthRejectionCounters(),
		});
	}

	if (method === "POST" && path === "/webhooks/github") {
		const body = await readBody(req);

		if (webhookSecret) {
			const sigHeader = (req.headers["x-hub-signature-256"] as string) || "";
			const valid = await verifyGitHubSignature(webhookSecret, new TextEncoder().encode(body), sigHeader);
			if (!valid) return json(res, { error: "invalid signature" }, 401);
		}

		const payload = parseJson(body);
		if (!payload) return json(res, { error: "invalid JSON" }, 400);

		const eventHeader = (req.headers["x-github-event"] as string) || "unknown";
		const deliveryId = (req.headers["x-github-delivery"] as string) || crypto.randomUUID();

		return respond(res, await handleGitHubWebhook(storage, eventHeader, deliveryId, payload));
	}

	if (method === "POST" && path === "/webhooks/linear") {
		const body = await readBody(req);
		const payload = parseJson(body);
		if (!payload) return json(res, { error: "invalid JSON" }, 400);
		return respond(res, await handleLinearWebhook(storage, payload));
	}

	if (method === "POST" && path === "/webhooks/slack") {
		const body = await readBody(req);
		const payload = parseJson(body);
		if (!payload) return json(res, { error: "invalid JSON" }, 400);

		// url_verification must be handled before BOTH the retry short-circuit
		// and the signature check: it carries no signing headers, and Slack
		// retries a failed handshake with x-slack-retry-num set — so swallowing
		// retries here would leave the request URL permanently unverified.
		if ((payload as Record<string, unknown>).type === "url_verification") {
			return json(res, { challenge: (payload as Record<string, unknown>).challenge });
		}

		// Dedup retried EVENT deliveries so the agent doesn't double-process.
		if (req.headers["x-slack-retry-num"]) {
			return json(res, { ok: true });
		}

		// Per-app signing secret (by api_app_id), falling back to the global one.
		const appSecret = await slackSigningSecretFor(
			storage,
			payload as Record<string, unknown>,
			slackSigningSecret,
		);
		if (appSecret) {
			const timestamp = (req.headers["x-slack-request-timestamp"] as string) || "";
			const signature = (req.headers["x-slack-signature"] as string) || "";
			const valid = await verifySlackSignature(appSecret, timestamp, body, signature);
			if (!valid) return json(res, { error: "invalid signature" }, 401);
		}

		return respond(res, await handleSlackWebhook(storage, payload));
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
		return respond(res, await handleSlackWorkspaceRegister(storage, data, bubbleId));
	}

	if (method === "POST" && path === "/slack/send") {
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
		if (!bubble) return json(res, { error: "forbidden" }, 403);
		return respond(res, await handleSlackSend(storage, data, bubble.id));
	}

	res.writeHead(404);
	res.end("Not Found");
}

// ---------------------------------------------------------------------------
// WebSocket upgrade — transport-specific (node ws)
// ---------------------------------------------------------------------------

function handleUpgrade(req: http.IncomingMessage, socket: import("node:net").Socket, head: Buffer, wss: WebSocketServer) {
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

		const lastSeen = parseInt(url.searchParams.get("last_seen") || "0", 10);
		if (lastSeen > 0) {
			for (const stored of dep.eventBuffer) {
				if (stored.seq > lastSeen) {
					try {
						ws.send(JSON.stringify({ type: "replay", data: stored }));
					} catch {
						break;
					}
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

const port = parseInt(process.env.MODASTACK_ES_PORT || "8080", 10);
const bind = process.env.MODASTACK_ES_BIND || "127.0.0.1";

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

server.listen(port, bind, () => {
	if (bind === "127.0.0.1" || bind === "::1") {
		console.log(
			`modastack event server (local) listening on ${bind}:${port} ` +
				`(loopback-only; set MODASTACK_ES_BIND to serve remotely)`,
		);
	} else {
		console.log(`modastack event server (local) listening on ${bind}:${port}`);
	}
});
