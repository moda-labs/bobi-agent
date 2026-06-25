import { DurableObject } from "cloudflare:workers";
import { constantTimeEqual, type NormalizedEvent, namespaceSubKey } from "./core";
import { INTERNAL_HEADER, internalSecretFromWebSocketProtocols } from "./internal-auth";

interface Env {
	EVENTS: KVNamespace;
	INTERNAL_DO_SECRET: string;
}

type StoredEvent = NormalizedEvent & { seq: number };

const EVENT_BUFFER_TTL = 48 * 60 * 60;

// Eviction backstop (#279): if all WebSockets disconnect and none
// reconnect within this window, the alarm fires and cleans up KV
// records so the deployment does not leak server-side.  Keyed on
// WS-disconnect, NOT activity — a live manager session can idle for
// hours but keeps its WS open, so it is never eligible for eviction.
const EVICTION_DELAY_MS = 60_000;

export class DeploymentSession extends DurableObject<Env> {
	private deploymentId: string = "";
	private nextSeq: number = 1;

	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		// Load persisted identity/sequence once; the DO is single-threaded,
		// so in-memory values are authoritative after this.
		ctx.blockConcurrencyWhile(async () => {
			this.deploymentId = ((await ctx.storage.get("deployment_id")) as string) || "";
			const savedSeq = (await ctx.storage.get("next_seq")) as number | undefined;
			if (savedSeq && savedSeq > this.nextSeq) {
				this.nextSeq = savedSeq;
			}
		});
	}

	override async fetch(request: Request): Promise<Response> {
		const expected = this.env.INTERNAL_DO_SECRET;
		const provided = request.headers.get(INTERNAL_HEADER)
			|| internalSecretFromWebSocketProtocols(request.headers.get("Sec-WebSocket-Protocol"));
		if (!expected || !provided || !constantTimeEqual(provided, expected)) {
			return new Response(null, { status: 403 });
		}

		const url = new URL(request.url);

		if (url.pathname === "/event" && request.method === "POST") {
			return this.handleIncomingEvent(request);
		}

		if (url.pathname === "/init" && request.method === "POST") {
			return this.handleInit(request);
		}

		const upgradeHeader = request.headers.get("Upgrade");
		if (upgradeHeader === "websocket") {
			return this.handleWebSocketUpgrade(request);
		}

		return new Response("Not Found", { status: 404 });
	}

	private async handleInit(request: Request): Promise<Response> {
		const body = (await request.json()) as { deployment_id: string; subscriptions: string[] };
		this.deploymentId = body.deployment_id;
		await this.ctx.storage.put("deployment_id", body.deployment_id);
		await this.ctx.storage.put("subscriptions", body.subscriptions);
		return new Response("OK");
	}

	private async handleIncomingEvent(request: Request): Promise<Response> {
		const event = (await request.json()) as NormalizedEvent;

		const seq = this.nextSeq++;
		const storedEvent: StoredEvent = { ...event, seq };

		await Promise.all([
			this.ctx.storage.put("next_seq", this.nextSeq),
			this.env.EVENTS.put(`events:${this.deploymentId}:${seq}`, JSON.stringify(storedEvent), {
				expirationTtl: EVENT_BUFFER_TTL,
			}),
		]);

		const message = JSON.stringify({ type: "event", data: storedEvent });
		for (const ws of this.ctx.getWebSockets()) {
			try {
				ws.send(message);
			} catch {
				// Managed by hibernation API
			}
		}

		return new Response("OK");
	}

	private async handleWebSocketUpgrade(request: Request): Promise<Response> {
		// Close stale WebSockets before accepting the new one (#322).
		// Reconnections are routine — Cloudflare cycles WS connections
		// regularly, and process restarts/session rotation do the same.
		// The old WS lingers in ctx.getWebSockets() until the runtime
		// detects the disconnect; during that window handleIncomingEvent
		// sends every event to BOTH sockets, producing duplicate
		// "Evaluating…" placeholders in Slack.
		for (const old of this.ctx.getWebSockets()) {
			try { old.close(1000, "replaced"); } catch { /* already closed */ }
		}

		const pair = new WebSocketPair();
		const [client, server] = [pair[0], pair[1]];

		this.ctx.acceptWebSocket(server);

		// A new WS connection means the deployment is alive — cancel any
		// pending eviction alarm.
		await this.ctx.storage.deleteAlarm();

		const url = new URL(request.url);
		const lastSeen = parseInt(url.searchParams.get("last_seen") || "0", 10);

		if (lastSeen > 0 && lastSeen < this.nextSeq - 1) {
			this.replayEvents(server, lastSeen);
		}

		server.send(
			JSON.stringify({
				type: "connected",
				deployment_id: this.deploymentId,
				next_seq: this.nextSeq,
			}),
		);

		return new Response(null, { status: 101, webSocket: client });
	}

	private async replayEvents(ws: WebSocket, afterSeq: number): Promise<void> {
		for (let seq = afterSeq + 1; seq < this.nextSeq; seq++) {
			const data = await this.env.EVENTS.get(`events:${this.deploymentId}:${seq}`);
			if (data) {
				try {
					ws.send(JSON.stringify({ type: "replay", data: JSON.parse(data) }));
				} catch {
					break;
				}
			}
		}
	}

	override async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
		if (typeof message !== "string") return;

		try {
			const msg = JSON.parse(message) as { type: string; seq?: number };

			if (msg.type === "ack" && typeof msg.seq === "number") {
				await this.ctx.storage.put("cursor", msg.seq);
			}

			if (msg.type === "ping") {
				ws.send(JSON.stringify({ type: "pong" }));
			}
		} catch {
			// Ignore malformed messages
		}
	}

	// When the last WebSocket disconnects, schedule an eviction alarm.
	// If a new WS connects before it fires, handleWebSocketUpgrade cancels it.
	override async webSocketClose(): Promise<void> {
		await this.scheduleEvictionIfDisconnected();
	}

	override async webSocketError(): Promise<void> {
		await this.scheduleEvictionIfDisconnected();
	}

	private async scheduleEvictionIfDisconnected(): Promise<void> {
		if (this.ctx.getWebSockets().length === 0) {
			await this.ctx.storage.setAlarm(Date.now() + EVICTION_DELAY_MS);
		}
	}

	// Alarm fires after the eviction delay.  If the deployment still has
	// no connected WebSockets, clean up KV records so the deployment does
	// not leak server-side.  The DO storage itself is ephemeral once the
	// deployment record is gone — Cloudflare will garbage-collect the DO.
	override async alarm(): Promise<void> {
		if (this.ctx.getWebSockets().length > 0) return; // reconnected in time

		if (!this.deploymentId) return;

		// Read the deployment record from KV to get the api_key for cleanup
		const depData = await this.env.EVENTS.get(`deployment_id:${this.deploymentId}`);
		if (!depData) return; // already cleaned up

		const dep = JSON.parse(depData) as {
			id: string;
			name: string;
			api_key: string;
			bubble_id: string;
			subscriptions: string[];
		};

		// Remove subscription-index entries — use namespaceSubKey from
		// core.ts so the namespace logic stays in one place.
		await Promise.all(
			dep.subscriptions.map(async (sub) => {
				const kvKey = `subscriptions:${namespaceSubKey(dep.bubble_id, sub)}`;
				const existing = await this.env.EVENTS.get(kvKey);
				if (!existing) return;
				const ids: string[] = JSON.parse(existing);
				const filtered = ids.filter((id) => id !== this.deploymentId);
				if (filtered.length === 0) {
					await this.env.EVENTS.delete(kvKey);
				} else {
					await this.env.EVENTS.put(kvKey, JSON.stringify(filtered));
				}
			}),
		);

		// Remove deployment records from KV — must mirror removeDeployment
		// in the KV storage adapter (index.ts), which deletes all three keys.
		await this.env.EVENTS.delete(`deployments:${dep.api_key}`);
		await this.env.EVENTS.delete(`deployment_id:${this.deploymentId}`);
		await this.env.EVENTS.delete(`deployment_name:${dep.bubble_id}:${dep.name}`);

		// Clear DO storage
		await this.ctx.storage.deleteAll();
	}
}
