import http from "node:http";
import { WebSocketServer, type WebSocket } from "ws";
import {
	type NormalizedEvent,
	createTopicEvent,
	normalizeGitHubPayload,
	normalizeLinearPayload,
	normalizeSlackPayload,
	subscriptionKeysForEvent,
	verifySlackSignature,
	verifyGitHubSignature,
} from "./core";

const MAX_BUFFER = 10_000;

interface Deployment {
	id: string;
	name: string;
	apiKey: string;
	subscriptions: string[];
	nextSeq: number;
	eventBuffer: Array<NormalizedEvent & { seq: number }>;
	websockets: Set<WebSocket>;
}

const deployments = new Map<string, Deployment>();
const apiKeyIndex = new Map<string, string>();
const subscriptionIndex = new Map<string, Set<string>>();

const webhookSecret = process.env.MODASTACK_ES_WEBHOOK_SECRET || "";
const slackSigningSecret = process.env.MODASTACK_ES_SLACK_SIGNING_SECRET || "";
const slackWorkspaces = new Map<string, string>(); // workspace_id -> bot_token

// ---------------------------------------------------------------------------
// Helpers
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

function authDeployment(deploymentId: string, req: http.IncomingMessage): Deployment | null {
	const auth = req.headers.authorization || "";
	if (!auth.startsWith("Bearer ")) return null;
	const apiKey = auth.slice(7);
	const depId = apiKeyIndex.get(apiKey);
	if (!depId || depId !== deploymentId) return null;
	return deployments.get(depId) || null;
}

// ---------------------------------------------------------------------------
// Event routing (in-memory equivalent of KV + Durable Objects)
// ---------------------------------------------------------------------------

function routeEvent(event: NormalizedEvent): number {
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
		if (dep.eventBuffer.length > MAX_BUFFER) {
			dep.eventBuffer.shift();
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
}

// ---------------------------------------------------------------------------
// HTTP handler
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

		let payload: Record<string, unknown>;
		try {
			payload = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		const eventHeader = (req.headers["x-github-event"] as string) || "unknown";
		const deliveryId = (req.headers["x-github-delivery"] as string) || crypto.randomUUID();

		const event = normalizeGitHubPayload(eventHeader, deliveryId, payload);
		if (!event) return json(res, { error: "no repository in payload" }, 400);

		return json(res, { delivered_to: routeEvent(event) });
	}

	if (method === "POST" && path === "/webhooks/linear") {
		const body = await readBody(req);
		let payload: Record<string, unknown>;
		try {
			payload = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		return json(res, { delivered_to: routeEvent(normalizeLinearPayload(payload)) });
	}

	if (method === "POST" && path === "/webhooks/slack") {
		if (req.headers["x-slack-retry-num"]) {
			return json(res, { ok: true });
		}

		const body = await readBody(req);
		let payload: Record<string, unknown>;
		try {
			payload = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		if ((payload as Record<string, unknown>).type === "url_verification") {
			return json(res, { challenge: (payload as Record<string, unknown>).challenge });
		}

		if (slackSigningSecret) {
			const timestamp = (req.headers["x-slack-request-timestamp"] as string) || "";
			const signature = (req.headers["x-slack-signature"] as string) || "";
			const valid = await verifySlackSignature(slackSigningSecret, timestamp, body, signature);
			if (!valid) return json(res, { error: "invalid signature" }, 401);
		}

		const result = normalizeSlackPayload(payload);

		if (result.challenge !== undefined) {
			return json(res, { challenge: result.challenge });
		}
		if (result.skip || !result.event) {
			return json(res, { ok: true });
		}

		return json(res, { delivered_to: routeEvent(result.event) });
	}

	if (method === "POST" && path === "/deployments") {
		const body = await readBody(req);
		let data: Record<string, unknown>;
		try {
			data = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		const name = data.name as string;
		const subs = data.subscriptions as string[];
		if (!name || !subs?.length) {
			return json(res, { error: "name and subscriptions[] required" }, 400);
		}

		const id = crypto.randomUUID();
		const apiKey = `moda_${crypto.randomUUID().replace(/-/g, "")}`;

		const dep: Deployment = {
			id,
			name,
			apiKey,
			subscriptions: [...subs],
			nextSeq: 1,
			eventBuffer: [],
			websockets: new Set(),
		};

		deployments.set(id, dep);
		apiKeyIndex.set(apiKey, id);
		for (const sub of subs) {
			if (!subscriptionIndex.has(sub)) subscriptionIndex.set(sub, new Set());
			subscriptionIndex.get(sub)!.add(id);
		}

		return json(res, { deployment_id: id, api_key: apiKey }, 201);
	}

	const subsMatch = path.match(/^\/deployments\/([^/]+)\/subscriptions$/);
	if (method === "PUT" && subsMatch) {
		const dep = authDeployment(subsMatch[1], req);
		if (!dep) return json(res, { error: "unauthorized" }, 403);

		const body = await readBody(req);
		let data: Record<string, unknown>;
		try {
			data = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		const newSubs = data.add as string[];
		if (!newSubs?.length) return json(res, { error: "add[] required" }, 400);

		let added = 0;
		for (const sub of newSubs) {
			if (!dep.subscriptions.includes(sub)) {
				dep.subscriptions.push(sub);
				added++;
			}
			if (!subscriptionIndex.has(sub)) subscriptionIndex.set(sub, new Set());
			subscriptionIndex.get(sub)!.add(dep.id);
		}

		return json(res, { subscriptions: dep.subscriptions, added });
	}

	// Generic topic: POST /events/{topic}
	const topicMatch = method === "POST" && path.match(/^\/events\/(.+)$/);
	if (topicMatch) {
		const body = await readBody(req);
		let data: Record<string, unknown>;
		try {
			data = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		const event = createTopicEvent(topicMatch[1], data);
		return json(res, { delivered_to: routeEvent(event) });
	}

	// Slack workspace registry: POST /slack/workspaces
	if (method === "POST" && path === "/slack/workspaces") {
		const body = await readBody(req);
		let data: Record<string, unknown>;
		try {
			data = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		const workspaceId = data.workspace_id as string;
		const botToken = data.bot_token as string;
		if (!workspaceId || !botToken) {
			return json(res, { error: "workspace_id and bot_token required" }, 400);
		}
		slackWorkspaces.set(workspaceId, botToken);
		return json(res, { ok: true, workspace_id: workspaceId });
	}

	// Slack send-through: POST /slack/send
	if (method === "POST" && path === "/slack/send") {
		const body = await readBody(req);
		let data: Record<string, unknown>;
		try {
			data = JSON.parse(body);
		} catch {
			return json(res, { error: "invalid JSON" }, 400);
		}

		const channel = data.channel as string;
		const text = data.text as string;
		if (!channel || !text) {
			return json(res, { error: "channel and text required" }, 400);
		}

		const botToken = slackWorkspaces.get((data.workspace as string) || "");
		if (!botToken) {
			return json(res, { error: "no bot token for workspace" }, 400);
		}

		const slackPayload: Record<string, unknown> = { channel, text };
		if (data.thread_ts) slackPayload.thread_ts = data.thread_ts;

		try {
			const resp = await fetch("https://slack.com/api/chat.postMessage", {
				method: "POST",
				headers: {
					Authorization: `Bearer ${botToken}`,
					"Content-Type": "application/json",
				},
				body: JSON.stringify(slackPayload),
			});
			const result = (await resp.json()) as Record<string, unknown>;
			if (!result.ok) {
				return json(res, { ok: false, error: result.error }, 502);
			}
			return json(res, { ok: true, ts: result.ts });
		} catch (err) {
			return json(res, { ok: false, error: String(err) }, 502);
		}
	}

	res.writeHead(404);
	res.end("Not Found");
}

// ---------------------------------------------------------------------------
// WebSocket upgrade
// ---------------------------------------------------------------------------

function handleUpgrade(req: http.IncomingMessage, socket: import("node:net").Socket, head: Buffer, wss: WebSocketServer) {
	const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
	const match = url.pathname.match(/^\/deployments\/([^/]+)\/subscribe$/);
	if (!match) {
		socket.destroy();
		return;
	}

	const deploymentId = match[1];
	const auth = req.headers.authorization || "";
	let token = auth.startsWith("Bearer ") ? auth.slice(7) : url.searchParams.get("token") || "";

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
