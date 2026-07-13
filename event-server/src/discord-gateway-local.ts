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
import { discordApiUrl } from "@moda-labs/bobi-events-core/channels";

const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 60_000;
// Watchdog for the connect -> HELLO -> READY/RESUMED handshake. Without it a
// half-open socket (suspend/resume, a proxy that accepts TCP but never
// upgrades, a gateway that never sends HELLO) parks the connection in
// "connecting" forever - no close event, no heartbeat, no recovery.
const HANDSHAKE_TIMEOUT_MS = 30_000;

export type DiscordConnectionState =
	| "connecting"
	| "connected"
	| "resuming"
	| "backoff"
	| "fatal";

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
	watchdogTimer: NodeJS.Timeout | null;
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
	 * Idempotent: re-registering the same token on a live connection is a
	 * no-op; a fatal connection restarts (re-registration is the documented
	 * recovery from a fixed configuration).
	 */
	start(applicationId: string, botToken: string): void {
		const existing = this.connections.get(applicationId);
		if (existing && existing.botToken === botToken && existing.state !== "fatal") {
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
			watchdogTimer: null,
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

	// GET /gateway/bot with explicit status handling: a 401/403 here is the
	// production bad-token signal (the socket's 4004 close never happens when
	// the authenticated REST call already fails) and must park the connection
	// as fatal instead of burning backoff retries forever.
	private async fetchGatewayInfo(
		token: string,
	): Promise<{ url: string; shards: number } | { fatal: string } | null> {
		const resp = await fetch(`${discordApiUrl()}gateway/bot`, {
			headers: { Authorization: `Bot ${token}` },
		});
		if (resp.status === 401 || resp.status === 403) {
			return { fatal: `GET /gateway/bot returned ${resp.status}: authentication failed (bad bot token)` };
		}
		if (!resp.ok) return null;
		const info = (await resp.json()) as Record<string, unknown>;
		const url = (info.url as string) || "";
		return url ? { url, shards: Number(info.shards ?? 1) } : null;
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
			let info: Awaited<ReturnType<DiscordGatewayManager["fetchGatewayInfo"]>>;
			try {
				info = await this.fetchGatewayInfo(conn.botToken);
			} catch (err) {
				if (conn.generation !== generation) return;
				console.error(`discord gateway: GET /gateway/bot failed for app `
					+ `${conn.applicationId}: ${String(err)}`);
				this.scheduleReconnect(conn, true);
				return;
			}
			if (conn.generation !== generation) return; // stopped/restarted meanwhile
			if (info && "fatal" in info) {
				this.fatal(conn, info.fatal);
				return;
			}
			if (!info) {
				this.scheduleReconnect(conn, true);
				return;
			}
			if (info.shards > 1) {
				// Sharding is non-scope in v1; connecting a single shard to a
				// bot Discord requires to shard would drop guild events
				// silently, so refuse loudly instead.
				this.fatal(conn, `bot requires ${info.shards} shards; sharding is not supported yet`);
				return;
			}
			url = info.url;
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
			// The session may still be resumable server-side; a local
			// constructor failure must not spend a fresh IDENTIFY (the
			// repeated-resume-failure fallback in scheduleReconnect covers a
			// permanently broken resume URL).
			this.scheduleReconnect(conn, true);
			return;
		}
		conn.ws = ws;

		// Handshake watchdog: cleared by the "connected" action (READY or
		// RESUMED); firing means the socket is half-open or HELLO never came.
		const watchdog = setTimeout(() => {
			if (conn.generation !== generation) return;
			console.error(`discord gateway: handshake timed out for app ${conn.applicationId}`);
			this.scheduleReconnect(conn, true);
		}, HANDSHAKE_TIMEOUT_MS);
		watchdog.unref();
		conn.watchdogTimer = watchdog;

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
					// One self-rescheduling timeout: first beat after
					// interval * jitter, then the steady interval.
					this.clearHeartbeat(conn);
					const tick = () => {
						if (conn.generation !== generation || !conn.session) return;
						this.apply(conn, generation, conn.session.onTimer("heartbeat"));
						const next = setTimeout(tick, action.intervalMs);
						next.unref();
						conn.heartbeatTimer = next;
					};
					const first = setTimeout(tick, action.firstDelayMs);
					first.unref();
					conn.heartbeatTimer = first;
					break;
				}

				case "connected":
					this.clearWatchdog(conn);
					conn.state = "connected";
					conn.backoffAttempt = 0;
					// Snapshot the resume state once per handshake so the
					// pre-session reconnect paths (e.g. a failing
					// /gateway/bot fetch) still hold a resumable session.
					conn.resume = conn.session?.state() ?? null;
					break;

				case "deliver":
					conn.lastEventAt = new Date().toISOString();
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
		this.clearWatchdog(conn);
		if (conn.reconnectTimer) {
			clearTimeout(conn.reconnectTimer);
			conn.reconnectTimer = null;
		}
		const ws = conn.ws;
		conn.ws = null;
		if (ws) {
			ws.removeAllListeners();
			// close() on a still-CONNECTING socket aborts the handshake and
			// emits 'error' on a later tick; with the listeners above removed
			// that emission would be an uncaught exception killing the whole
			// server, so a sink listener must stay attached.
			ws.on("error", () => { /* sink - socket is being discarded */ });
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
			conn.heartbeatTimer = null;
		}
	}

	private clearWatchdog(conn: Connection): void {
		if (conn.watchdogTimer) {
			clearTimeout(conn.watchdogTimer);
			conn.watchdogTimer = null;
		}
	}
}
