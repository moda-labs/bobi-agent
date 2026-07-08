/**
 * Discord Gateway protocol core - sans-IO state machine.
 *
 * Discord delivers MESSAGE_CREATE only over the Gateway, a persistent
 * WebSocket the client opens and maintains
 * (https://discord.com/developers/docs/events/gateway). This module owns the
 * protocol - opcodes, IDENTIFY/RESUME, heartbeat bookkeeping, close-code
 * policy - while the runtime drivers own sockets and timers. Both runtimes
 * (local Node process now, Durable Object later) share this one code path,
 * and it unit-tests with recorded frames, no network.
 *
 * Inputs are the three things a driver observes (a frame, a timer firing, a
 * socket close); the output is a list of GatewayActions the driver executes
 * mechanically. The session never touches a socket.
 */
import type { NormalizedEvent } from "../core";
import { normalizeDiscordMessage } from "../adapters/discord";

// Gateway opcodes (https://discord.com/developers/docs/topics/opcodes-and-status-codes).
export const GatewayOp = {
	DISPATCH: 0,
	HEARTBEAT: 1,
	IDENTIFY: 2,
	RESUME: 6,
	RECONNECT: 7,
	INVALID_SESSION: 9,
	HELLO: 10,
	HEARTBEAT_ACK: 11,
} as const;

// Gateway intents. MESSAGE_CONTENT is privileged - DMs and bot-mentions are
// exempt, so the v1 surface works without it; identifying with an intent not
// enabled in the developer portal closes the socket with 4014, so it is
// strictly opt-in.
export const DiscordIntents = {
	GUILDS: 1 << 0,
	GUILD_MESSAGES: 1 << 9,
	DIRECT_MESSAGES: 1 << 12,
	MESSAGE_CONTENT: 1 << 15,
} as const;

export const DEFAULT_DISCORD_INTENTS =
	DiscordIntents.GUILDS | DiscordIntents.GUILD_MESSAGES | DiscordIntents.DIRECT_MESSAGES;

// Close codes that must NOT be retried: a bad token or a disallowed intent
// never fixes itself, and a reconnect loop would burn the 1000/day IDENTIFY
// budget. The driver parks the connection and surfaces unhealthy.
const FATAL_CLOSE_REASONS: Record<number, string> = {
	4004: "authentication failed (bad bot token)",
	4010: "invalid shard sent to the gateway",
	4011: "sharding required (bot is in 2500+ guilds; sharding is non-scope in v1)",
	4012: "invalid gateway API version",
	4013: "invalid intents",
	4014: "disallowed intents (enable them in the Discord developer portal)",
};

// Close codes after which the session is gone and only a fresh IDENTIFY
// works (clean closes invalidate the session; 4007 = invalid seq, 4009 =
// session timed out). Everything else non-fatal resumes.
const NON_RESUMABLE_CLOSE_CODES = new Set([1000, 1001, 4007, 4009]);

/** Resume state a driver may persist between connections. */
export interface GatewayResumeState {
	sessionId: string;
	resumeGatewayUrl: string;
	lastSeq: number | null;
	// The connection's own bot user id. Only READY carries it (RESUMED does
	// not), so it must survive resumes - the normalizer needs it to classify
	// mentions and replies.
	botUserId: string;
}

export type GatewayAction =
	| { kind: "send"; frame: string }
	| { kind: "setHeartbeat"; intervalMs: number; firstDelayMs: number }
	// Driver closes the socket (if still open) and reconnects: to
	// state().resumeGatewayUrl when resume is true, else fresh via
	// GET /gateway/bot. Resume-first is mandatory (IDENTIFY budget), not an
	// optimization.
	| { kind: "reconnect"; resume: boolean }
	// Handshake completed (READY or RESUMED) - the driver resets its backoff.
	| { kind: "connected"; resumed: boolean }
	| { kind: "deliver"; events: NormalizedEvent[] }
	| { kind: "fatal"; reason: string };

export interface DiscordGatewaySessionOptions {
	token: string;
	applicationId: string;
	intents?: number;
	/** Persisted resume state; presence makes the session RESUME on HELLO. */
	resume?: GatewayResumeState | null;
	/** Injectable jitter source for deterministic tests. */
	random?: () => number;
}

export class DiscordGatewaySession {
	private readonly token: string;
	private readonly applicationId: string;
	private readonly intents: number;
	private readonly random: () => number;

	private sessionId: string;
	private resumeGatewayUrl: string;
	private lastSeq: number | null;
	private resuming: boolean;
	private awaitingAck = false;
	private ready = false;
	private botUser = "";

	constructor(opts: DiscordGatewaySessionOptions) {
		this.token = opts.token;
		this.applicationId = opts.applicationId;
		this.intents = opts.intents ?? DEFAULT_DISCORD_INTENTS;
		this.random = opts.random ?? Math.random;
		this.sessionId = opts.resume?.sessionId ?? "";
		this.resumeGatewayUrl = opts.resume?.resumeGatewayUrl ?? "";
		this.lastSeq = opts.resume?.lastSeq ?? null;
		this.botUser = opts.resume?.botUserId ?? "";
		this.resuming = Boolean(opts.resume?.sessionId);
	}

	/** Resume state to persist, or null when only a fresh IDENTIFY works. */
	state(): GatewayResumeState | null {
		if (!this.sessionId || !this.resumeGatewayUrl) return null;
		return {
			sessionId: this.sessionId,
			resumeGatewayUrl: this.resumeGatewayUrl,
			lastSeq: this.lastSeq,
			botUserId: this.botUser,
		};
	}

	/** Whether the READY/RESUMED handshake completed on this connection. */
	isReady(): boolean {
		return this.ready;
	}

	/** The connection's own bot user id (known after READY). */
	botUserId(): string {
		return this.botUser;
	}

	/** One Gateway payload received from the socket. */
	onFrame(raw: string): GatewayAction[] {
		let frame: Record<string, unknown>;
		try {
			frame = JSON.parse(raw) as Record<string, unknown>;
		} catch {
			return []; // a malformed frame must not kill the connection
		}
		if (!frame || typeof frame !== "object") return [];

		if (typeof frame.s === "number") this.lastSeq = frame.s;

		switch (frame.op) {
			case GatewayOp.HELLO: {
				const d = (frame.d as Record<string, unknown>) || {};
				const intervalMs = Number(d.heartbeat_interval);
				if (!Number.isFinite(intervalMs) || intervalMs <= 0) {
					return [{ kind: "reconnect", resume: this.resuming }];
				}
				this.awaitingAck = false;
				return [
					// First heartbeat after interval * jitter (per the docs), so a
					// fleet of clients never beats in lockstep.
					{ kind: "setHeartbeat", intervalMs, firstDelayMs: Math.floor(intervalMs * this.random()) },
					{ kind: "send", frame: this.resuming ? this.resumeFrame() : this.identifyFrame() },
				];
			}

			case GatewayOp.HEARTBEAT:
				// The server may request an immediate beat at any time.
				return [{ kind: "send", frame: this.heartbeatFrame() }];

			case GatewayOp.HEARTBEAT_ACK:
				this.awaitingAck = false;
				return [];

			case GatewayOp.RECONNECT:
				// Discord asks us to reconnect (deploys, rebalancing); the session
				// stays resumable.
				return [{ kind: "reconnect", resume: this.state() !== null }];

			case GatewayOp.INVALID_SESSION: {
				// d indicates whether the session is still resumable.
				const resumable = frame.d === true;
				if (!resumable) this.clearSession();
				return [{ kind: "reconnect", resume: resumable }];
			}

			case GatewayOp.DISPATCH:
				return this.onDispatch(
					(frame.t as string) || "",
					(frame.d as Record<string, unknown>) || {},
				);

			default:
				return [];
		}
	}

	/** The driver's heartbeat timer fired. */
	onTimer(kind: "heartbeat"): GatewayAction[] {
		if (kind !== "heartbeat") return [];
		if (this.awaitingAck) {
			// Missed ACK = zombie connection: close with a non-1000 code and
			// resume (the docs' prescribed recovery).
			return [{ kind: "reconnect", resume: this.state() !== null }];
		}
		this.awaitingAck = true;
		return [{ kind: "send", frame: this.heartbeatFrame() }];
	}

	/** The socket closed with `code`. Decides fatal vs resume vs re-IDENTIFY. */
	onSocketClose(code: number): GatewayAction[] {
		const fatal = FATAL_CLOSE_REASONS[code];
		if (fatal) return [{ kind: "fatal", reason: `close ${code}: ${fatal}` }];
		if (NON_RESUMABLE_CLOSE_CODES.has(code)) this.clearSession();
		return [{ kind: "reconnect", resume: this.state() !== null }];
	}

	private onDispatch(t: string, d: Record<string, unknown>): GatewayAction[] {
		if (t === "READY") {
			this.sessionId = (d.session_id as string) || "";
			this.resumeGatewayUrl = (d.resume_gateway_url as string) || "";
			this.botUser = ((d.user as Record<string, unknown>)?.id as string) || "";
			this.ready = true;
			this.resuming = false;
			return [{ kind: "connected", resumed: false }];
		}
		if (t === "RESUMED") {
			this.ready = true;
			this.resuming = false;
			return [{ kind: "connected", resumed: true }];
		}
		if (t === "MESSAGE_CREATE") {
			const events = normalizeDiscordMessage(d, this.botUser, this.applicationId);
			return events.length > 0 ? [{ kind: "deliver", events }] : [];
		}
		return [];
	}

	private clearSession(): void {
		this.sessionId = "";
		this.resumeGatewayUrl = "";
		this.lastSeq = null;
		this.resuming = false;
	}

	private identifyFrame(): string {
		return JSON.stringify({
			op: GatewayOp.IDENTIFY,
			d: {
				token: this.token,
				intents: this.intents,
				properties: { os: "linux", browser: "bobi", device: "bobi" },
				// Single shard by design; the driver refuses loudly when
				// GET /gateway/bot reports shards > 1.
				shard: [0, 1],
			},
		});
	}

	private resumeFrame(): string {
		return JSON.stringify({
			op: GatewayOp.RESUME,
			d: {
				token: this.token,
				session_id: this.sessionId,
				seq: this.lastSeq ?? 0,
			},
		});
	}

	private heartbeatFrame(): string {
		return JSON.stringify({ op: GatewayOp.HEARTBEAT, d: this.lastSeq });
	}
}
