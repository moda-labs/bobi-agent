/**
 * Discord Gateway driver for the local Node event server.
 *
 * Runs inside the existing local process (the one bobi/events/server.py
 * ensure_running() spawns): it shares the in-memory StorageAdapter so
 * delivery is an in-process call, and it inherits the existing
 * spawn/health/eviction lifecycle for free. One Gateway connection per
 * registered bot token; the sans-IO session (core/gateway/discord.ts) owns
 * the protocol, this driver owns sockets, timers, and backoff.
 *
 * Resume state lives in memory only: a process restart re-IDENTIFYs, which
 * is comfortably within Discord's 1000/day session budget.
 */
import WebSocket from "ws";
import type { StorageAdapter } from "@moda-labs/bobi-events-core";
import {
	DiscordGatewaySession,
	DiscordIntents,
	DEFAULT_DISCORD_INTENTS,
	type GatewayAction,
	type GatewayResumeState,
} from "@moda-labs/bobi-events-core/gateway/discord";
import { discordApi } from "@moda-labs/bobi-events-core/channels";

const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 60_000;

export type DiscordConnectionState =
	| "connecting"
	| "connected"
	| "resuming"
	| "backoff"
	| "fatal"
	| "stopped";

export interface DiscordConnectionHealth {
	application_id: string;
	state: DiscordConnectionState;
	last_event_at: string | null;
	identify_count: number;
	resume_count: number;
	fatal_reason?: string;
}

export interface DiscordGatewayOptions {
	/** Extra intents beyond the default set (e.g. MESSAGE_CONTENT, opt-in). */
	messageContent?: boolean;
	/** Test seam: connect here instead of asking GET /gateway/bot. */
	gatewayUrlOverride?: string;
}

interface Connection {
	applicationId: string;
	botToken: string;
	state: DiscordConnectionState;
	session: DiscordGatewaySession | null;
	ws: WebSocket | null;
	resume: GatewayResumeState | null;
	heartbeatTimer: NodeJS.Timeout | null;
	reconnectTimer: NodeJS.Timeout | null;
	backoffAttempt: number;
	lastEventAt: string | null;
	identifyCount: number;
	resumeCount: number;
	fatalReason: string;
	/** Bumped on stop()/restart so a stale socket's callbacks become no-ops. */
	generation: number;
}

export class DiscordGatewayManager {
	private readonly storage: StorageAdapter;
	private readonly intents: number;
	private readonly gatewayUrlOverride: string;
	private readonly connections = new Map<string, Connection>();

	constructor(storage: StorageAdapter, opts: DiscordGatewayOptions = {}) {
		this.storage = storage;
		this.intents = DEFAULT_DISCORD_INTENTS
			| (opts.messageContent ? DiscordIntents.MESSAGE_CONTENT : 0);
		this.gatewayUrlOverride = opts.gatewayUrlOverride ?? "";
	}

	/**
	 * Start (or restart with a fresh token) the connection for one app.
	 * Idempotent: re-registering the same token is a no-op.
	 */
	start(applicationId: string, botToken: string): void {
		const existing = this.connections.get(applicationId);
		if (existing && existing.botToken === botToken && existing.state !== "fatal"
			&& existing.state !== "stopped") {
			return;
		}
		if (existing) this.teardown(existing);
		const conn: Connection = {
			applicationId,
			botToken,
			state: "connecting",
			session: null,
			ws: null,
			resume: null,
			heartbeatTimer: null,
			reconnectTimer: null,
			backoffAttempt: 0,
			lastEventAt: null,
			identifyCount: 0,
			resumeCount: 0,
			fatalReason: "",
			generation: (existing?.generation ?? 0) + 1,
		};
		this.connections.set(applicationId, conn);
		void this.connect(conn);
	}

	stop(applicationId: string): void {
		const conn = this.connections.get(applicationId);
		if (!conn) return;
		this.teardown(conn);
		conn.state = "stopped";
		this.connections.delete(applicationId);
	}

	stopAll(): void {
		for (const id of [...this.connections.keys()]) this.stop(id);
	}

	health(): DiscordConnectionHealth[] {
		return [...this.connections.values()].map((c) => ({
			application_id: c.applicationId,
			state: c.state,
			last_event_at: c.lastEventAt,
			identify_count: c.identifyCount,
			resume_count: c.resumeCount,
			...(c.fatalReason ? { fatal_reason: c.fatalReason } : {}),
		}));
	}

	private async connect(conn: Connection): Promise<void> {
		const generation = conn.generation;
		conn.state = conn.resume ? "resuming" : "connecting";

		let url: string;
		if (conn.resume) {
			url = conn.resume.resumeGatewayUrl;
		} else if (this.gatewayUrlOverride) {
			url = this.gatewayUrlOverride;
		} else {
			try {
				const info = await discordApi(conn.botToken, "gateway/bot", { method: "GET" });
				if (conn.generation !== generation) return; // stopped/restarted meanwhile
				const shards = Number(info.shards ?? 1);
				if (shards > 1) {
					// Sharding is non-scope in v1; connecting a single shard to a
					// bot Discord requires to shard would drop guild events
					// silently, so refuse loudly instead.
					this.fatal(conn, `bot requires ${shards} shards; sharding is not supported yet`);
					return;
				}
				url = (info.url as string) || "";
				if (!url) {
					this.scheduleReconnect(conn, true);
					return;
				}
			} catch (err) {
				if (conn.generation !== generation) return;
				console.error(`discord gateway: GET /gateway/bot failed for app `
					+ `${conn.applicationId}: ${String(err)}`);
				this.scheduleReconnect(conn, true);
				return;
			}
		}

		const session = new DiscordGatewaySession({
			token: conn.botToken,
			applicationId: conn.applicationId,
			intents: this.intents,
			resume: conn.resume,
		});
		conn.session = session;
		if (conn.resume) conn.resumeCount++;
		else conn.identifyCount++;

		let ws: WebSocket;
		try {
			ws = new WebSocket(`${url}${url.includes("?") ? "&" : "?"}v=10&encoding=json`);
		} catch (err) {
			console.error(`discord gateway: connect failed for app `
				+ `${conn.applicationId}: ${String(err)}`);
			this.scheduleReconnect(conn, false);
			return;
		}
		conn.ws = ws;

		ws.on("message", (data) => {
			if (conn.generation !== generation) return;
			this.apply(conn, generation, session.onFrame(String(data)));
		});
		ws.on("close", (code) => {
			if (conn.generation !== generation || conn.ws !== ws) return;
			this.clearHeartbeat(conn);
			this.apply(conn, generation, session.onSocketClose(code || 1006));
		});
		ws.on("error", (err) => {
			// The close event follows and drives the reconnect; just log.
			console.error(`discord gateway: socket error for app `
				+ `${conn.applicationId}: ${String(err)}`);
		});
	}

	private apply(conn: Connection, generation: number, actions: GatewayAction[]): void {
		for (const action of actions) {
			if (conn.generation !== generation) return;
			switch (action.kind) {
				case "send":
					try {
						conn.ws?.send(action.frame);
					} catch (err) {
						console.error(`discord gateway: send failed for app `
							+ `${conn.applicationId}: ${String(err)}`);
					}
					break;

				case "setHeartbeat": {
					this.clearHeartbeat(conn);
					const beat = () => {
						if (conn.generation !== generation || !conn.session) return;
						this.apply(conn, generation, conn.session.onTimer("heartbeat"));
					};
					// First beat after interval * jitter, then steady interval.
					const first = setTimeout(() => {
						beat();
						const interval = setInterval(beat, action.intervalMs);
						interval.unref();
						conn.heartbeatTimer = interval;
					}, action.firstDelayMs);
					first.unref();
					conn.heartbeatTimer = first;
					break;
				}

				case "connected":
					conn.state = "connected";
					conn.backoffAttempt = 0;
					conn.resume = conn.session?.state() ?? null;
					break;

				case "deliver":
					conn.lastEventAt = new Date().toISOString();
					conn.resume = conn.session?.state() ?? null;
					for (const event of action.events) {
						void this.storage.deliver(event).catch((err) => {
							console.error(`discord gateway: delivery failed for event `
								+ `${event.id}: ${String(err)}`);
						});
					}
					break;

				case "reconnect":
					this.scheduleReconnect(conn, action.resume);
					return; // the session on this socket is done

				case "fatal":
					this.fatal(conn, action.reason);
					return;
			}
		}
	}

	private scheduleReconnect(conn: Connection, resume: boolean): void {
		const generation = conn.generation;
		this.closeSocket(conn);
		// A resume endpoint that keeps failing is gone (Discord rotates them);
		// after a few attempts fall back to a fresh IDENTIFY via /gateway/bot
		// rather than retrying a dead URL forever.
		if (resume && conn.backoffAttempt >= 5) resume = false;
		conn.resume = resume ? (conn.session?.state() ?? conn.resume) : null;
		conn.session = null;
		conn.state = "backoff";
		// Exponential backoff with jitter, capped. Resume-first keeps fresh
		// IDENTIFYs (budgeted at 1000/day) to genuine session losses.
		const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** conn.backoffAttempt)
			* (0.5 + Math.random() * 0.5);
		conn.backoffAttempt = Math.min(conn.backoffAttempt + 1, 10);
		const timer = setTimeout(() => {
			if (conn.generation !== generation) return;
			void this.connect(conn);
		}, delay);
		timer.unref();
		conn.reconnectTimer = timer;
	}

	private fatal(conn: Connection, reason: string): void {
		this.closeSocket(conn);
		conn.session = null;
		conn.resume = null;
		conn.state = "fatal";
		conn.fatalReason = reason;
		console.error(`discord gateway: FATAL for app ${conn.applicationId}: ${reason} `
			+ `- not reconnecting; fix the configuration and re-register`);
	}

	private teardown(conn: Connection): void {
		conn.generation++;
		this.closeSocket(conn);
		conn.session = null;
	}

	private closeSocket(conn: Connection): void {
		this.clearHeartbeat(conn);
		if (conn.reconnectTimer) {
			clearTimeout(conn.reconnectTimer);
			conn.reconnectTimer = null;
		}
		const ws = conn.ws;
		conn.ws = null;
		if (ws) {
			ws.removeAllListeners();
			try {
				// Non-1000 close code keeps the session resumable server-side.
				ws.close(4000, "reconnecting");
			} catch {
				try { ws.terminate(); } catch { /* already gone */ }
			}
		}
	}

	private clearHeartbeat(conn: Connection): void {
		if (conn.heartbeatTimer) {
			clearTimeout(conn.heartbeatTimer);
			clearInterval(conn.heartbeatTimer);
			conn.heartbeatTimer = null;
		}
	}
}
