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
	verifyGitHubSignature,
	verifySlackSignature,
	readBubbleAuthHeaders,
	handleGitHubWebhook,
	handleLinearWebhook,
	handleSlackWebhook,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleTopicEvent,
	handleSlackSend,
	handleSlackWorkspaceRegister,
} from "./core";

const MAX_BUFFER = 10_000;

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

	async addSubscription(key: string, deploymentId: string): Promise<void> {
		if (!subscriptionIndex.has(key)) subscriptionIndex.set(key, new Set());
		subscriptionIndex.get(key)!.add(deploymentId);
	},

	async deliver(event: NormalizedEvent): Promise<number> {
		const keys = subscriptionKeysForEvent(event);
		const depIds = new Set<string>();
		for (const key of keys) {
			for (const id of subscriptionIndex.get(key) || []) {
				depIds.add(id);
			}
		}

		let delivered = 0;
		for (const depId of depIds) {
			const dep = deployments.get(depId);
			if (!dep) continue;

			const seq = dep.nextSeq++;
			const seqEvent = { ...event, seq };
			dep.eventBuffer.push(seqEvent);
			// Amortized O(1) eviction — shift() on a full 10k buffer copies the
			// whole array on every event.
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
		return json(res, { status: "ok", mode: "local", deployments: deployments.size });
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
		if (req.headers["x-slack-retry-num"]) {
			return json(res, { ok: true });
		}

		const body = await readBody(req);
		const payload = parseJson(body);
		if (!payload) return json(res, { error: "invalid JSON" }, 400);

		// url_verification must be handled before signature check —
		// Slack's url_verification request does not include signing headers.
		if ((payload as Record<string, unknown>).type === "url_verification") {
			return json(res, { challenge: (payload as Record<string, unknown>).challenge });
		}

		if (slackSigningSecret) {
			const timestamp = (req.headers["x-slack-request-timestamp"] as string) || "";
			const signature = (req.headers["x-slack-signature"] as string) || "";
			const valid = await verifySlackSignature(slackSigningSecret, timestamp, body, signature);
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
		return respond(res, await handleSlackWorkspaceRegister(storage, data));
	}

	if (method === "POST" && path === "/slack/send") {
		const body = await readBody(req);
		const data = parseJson(body);
		if (!data) return json(res, { error: "invalid JSON" }, 400);
		return respond(res, await handleSlackSend(storage, data));
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

		ws.on("close", () => dep.websockets.delete(ws));
		ws.on("error", () => dep.websockets.delete(ws));
	});
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.MODASTACK_ES_PORT || "8080", 10);

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

server.listen(port, () => {
	console.log(`modastack event server (local) listening on port ${port}`);
});
