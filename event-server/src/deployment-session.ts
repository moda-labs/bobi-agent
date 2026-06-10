import { DurableObject } from "cloudflare:workers";
import type { NormalizedEvent } from "./core";

interface Env {
	EVENTS: KVNamespace;
}

type StoredEvent = NormalizedEvent & { seq: number };

const EVENT_BUFFER_TTL = 48 * 60 * 60;

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
		const pair = new WebSocketPair();
		const [client, server] = [pair[0], pair[1]];

		this.ctx.acceptWebSocket(server);

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

	override async webSocketClose(): Promise<void> {
		// Managed by hibernation API
	}

	override async webSocketError(): Promise<void> {
		// Managed by hibernation API
	}
}
