import { DurableObject } from "cloudflare:workers";

interface Env {
	EVENTS: KVNamespace;
}

interface StoredEvent {
	seq: number;
	id: string;
	source: string;
	type: string;
	timestamp: string;
	repo?: string;
	team_key?: string;
	workspace?: string;
	payload: Record<string, unknown>;
}

const EVENT_BUFFER_TTL = 48 * 60 * 60; // 48 hours in seconds

export class DeploymentSession extends DurableObject<Env> {
	private deploymentId: string = "";
	private nextSeq: number = 1;

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
		const body = await request.json() as { deployment_id: string; subscriptions: string[] };
		this.deploymentId = body.deployment_id;
		await this.ctx.storage.put("deployment_id", body.deployment_id);
		await this.ctx.storage.put("subscriptions", body.subscriptions);
		return new Response("OK");
	}

	private async handleIncomingEvent(request: Request): Promise<Response> {
		const event = await request.json() as Record<string, unknown>;

		if (!this.deploymentId) {
			this.deploymentId = (await this.ctx.storage.get("deployment_id")) as string || "";
		}

		const savedSeq = (await this.ctx.storage.get("next_seq")) as number | undefined;
		if (savedSeq && savedSeq > this.nextSeq) {
			this.nextSeq = savedSeq;
		}

		const seq = this.nextSeq++;
		await this.ctx.storage.put("next_seq", this.nextSeq);

		const storedEvent: StoredEvent = {
			seq,
			id: event.id as string,
			source: event.source as string,
			type: event.type as string,
			timestamp: event.timestamp as string,
			repo: event.repo as string | undefined,
			team_key: event.team_key as string | undefined,
			workspace: event.workspace as string | undefined,
			payload: event.payload as Record<string, unknown>,
		};

		await this.env.EVENTS.put(
			`events:${this.deploymentId}:${seq}`,
			JSON.stringify(storedEvent),
			{ expirationTtl: EVENT_BUFFER_TTL },
		);

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
		if (!this.deploymentId) {
			this.deploymentId = (await this.ctx.storage.get("deployment_id")) as string || "";
		}

		const savedSeq = (await this.ctx.storage.get("next_seq")) as number | undefined;
		if (savedSeq) {
			this.nextSeq = savedSeq;
		}

		const pair = new WebSocketPair();
		const [client, server] = [pair[0], pair[1]];

		this.ctx.acceptWebSocket(server);

		const url = new URL(request.url);
		const lastSeen = parseInt(url.searchParams.get("last_seen") || "0", 10);

		if (lastSeen > 0 && lastSeen < this.nextSeq - 1) {
			this.replayEvents(server, lastSeen);
		}

		server.send(JSON.stringify({
			type: "connected",
			deployment_id: this.deploymentId,
			next_seq: this.nextSeq,
		}));

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
