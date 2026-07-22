/** Slack Socket Mode driver for the local Node event server. */
import WebSocket from "ws";
import { handleSlackWebhook } from "@moda-labs/bobi-events-core";
import type { HandlerResult, StorageAdapter } from "@moda-labs/bobi-events-core";
import { slackApiUrl } from "@moda-labs/bobi-events-core/channels";
import {
	AcknowledgedEnvelopeCache,
	SlackSocketSession,
	type SlackSocketAction,
} from "@moda-labs/bobi-events-core/gateway/slack-socket";
import {
	calculateBackoffDelay,
	clearScheduledTimeout,
	disposeWebSocket,
	isCurrentGeneration,
	scheduleUnrefTimeout,
} from "./socket-driver-common";

const BACKOFF_BASE_MS = 5_000;
const BACKOFF_MAX_MS = 60_000;
const HANDSHAKE_TIMEOUT_MS = 30_000;
const STALENESS_TIMEOUT_MS = 30_000;
const REST_TIMEOUT_MS = 10_000;
const MAX_PAYLOAD_BYTES = 1_048_576;
const MAX_CONCURRENT_DELIVERIES = 16;
const MAX_QUEUED_DELIVERIES = 256;

const FATAL_SLACK_ERRORS = new Set([
	"account_inactive",
	"invalid_auth",
	"missing_scope",
	"not_allowed_token_type",
	"not_authed",
	"token_expired",
	"token_revoked",
]);

export type SlackConnectionState = "connecting" | "connected" | "backoff" | "fatal";

export interface SlackConnectionHealth {
	application_id: string | null;
	state: SlackConnectionState;
	last_event_at: string | null;
	delivered_event_count: number;
	connect_count: number;
	reconnect_count: number;
	fatal_reason?: string;
}

export interface SlackSocketRegistration {
	/** Non-secret identity used until hello confirms the Slack application id. */
	registrationId: string;
	appToken: string;
	applicationId?: string;
}

export interface SlackSocketManagerOptions {
	bindAddress?: string;
	apiUrlIsOverride?: boolean;
	random?: () => number;
	handshakeTimeoutMs?: number;
	stalenessTimeoutMs?: number;
	restTimeoutMs?: number;
	maxConcurrentDeliveries?: number;
	maxQueuedDeliveries?: number;
	/** Test seam for deterministic driver tests; production uses `ws`. */
	webSocketFactory?: (url: string, options: { maxPayload: number }) => WebSocket;
}

export type SlackBootstrapClassification =
	| { kind: "success" }
	| { kind: "retry"; retryAfterMs: number }
	| { kind: "fatal"; reason: string };

interface PendingAcknowledgement {
	fail(): void;
}

interface Connection {
	key: string;
	registrationId: string;
	expectedApplicationId: string | null;
	applicationId: string | null;
	appToken: string;
	state: SlackConnectionState;
	session: SlackSocketSession | null;
	ws: WebSocket | null;
	acknowledgedEnvelopes: AcknowledgedEnvelopeCache;
	handshakeTimer: NodeJS.Timeout | null;
	stalenessTimer: NodeJS.Timeout | null;
	reconnectTimer: NodeJS.Timeout | null;
	backoffAttempt: number;
	lastEventAt: string | null;
	deliveredEventCount: number;
	connectCount: number;
	reconnectCount: number;
	fatalReason: string;
	generation: number;
	pendingAcknowledgements: Set<PendingAcknowledgement>;
}

interface AcceptedDelivery {
	conn: Connection;
	generation: number;
	envelopeId: string;
	payload: Record<string, unknown>;
}

function record(value: unknown): Record<string, unknown> | null {
	return value !== null && typeof value === "object" && !Array.isArray(value)
		? value as Record<string, unknown>
		: null;
}

function retryAfterMs(value: string | null): number {
	if (!value) return 0;
	const seconds = Number(value);
	if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds * 1_000);
	const at = Date.parse(value);
	return Number.isFinite(at) ? Math.max(0, at - Date.now()) : 0;
}

export function classifySlackBootstrapResponse(
	status: number,
	body: unknown,
	retryAfter: string | null,
): SlackBootstrapClassification {
	const data = record(body);
	const error = typeof data?.error === "string" ? data.error : "";
	const retryMs = retryAfterMs(retryAfter);
	if (status === 429 || error === "rate_limited") {
		return { kind: "retry", retryAfterMs: retryMs };
	}
	if ((status >= 400 && status < 500) || FATAL_SLACK_ERRORS.has(error)) {
		return {
			kind: "fatal",
			reason: "apps.connections.open rejected the app credential or scope",
		};
	}
	if (status >= 200 && status < 300 && data?.ok === true
		&& typeof data.url === "string" && data.url.length > 0) {
		return { kind: "success" };
	}
	return { kind: "retry", retryAfterMs: retryMs };
}

export function validateSlackSocketUrl(value: string): boolean {
	try {
		const url = new URL(value);
		return url.protocol === "wss:"
			&& url.hostname.length > 0
			&& url.username.length === 0
			&& url.password.length === 0;
	} catch {
		return false;
	}
}

export function isLoopbackBind(value: string): boolean {
	const normalized = value.trim().toLowerCase();
	if (normalized === "localhost" || normalized === "::1") return true;
	const octets = normalized.split(".");
	return octets.length === 4
		&& octets[0] === "127"
		&& octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255);
}

export class SlackSocketManager {
	private readonly storage: StorageAdapter;
	private readonly bindAddress: string;
	private readonly apiUrlIsOverride: boolean;
	private readonly random: () => number;
	private readonly handshakeTimeoutMs: number;
	private readonly stalenessTimeoutMs: number;
	private readonly restTimeoutMs: number;
	private readonly maxConcurrentDeliveries: number;
	private readonly maxQueuedDeliveries: number;
	private readonly webSocketFactory: (url: string, options: { maxPayload: number }) => WebSocket;
	private readonly connections = new Map<string, Connection>();
	private readonly deliveryQueue: AcceptedDelivery[] = [];
	private activeDeliveries = 0;
	private reservedDeliveries = 0;

	constructor(storage: StorageAdapter, opts: SlackSocketManagerOptions = {}) {
		this.storage = storage;
		this.bindAddress = opts.bindAddress ?? "127.0.0.1";
		this.apiUrlIsOverride = opts.apiUrlIsOverride ?? false;
		this.random = opts.random ?? Math.random;
		this.handshakeTimeoutMs = opts.handshakeTimeoutMs ?? HANDSHAKE_TIMEOUT_MS;
		this.stalenessTimeoutMs = opts.stalenessTimeoutMs ?? STALENESS_TIMEOUT_MS;
		this.restTimeoutMs = opts.restTimeoutMs ?? REST_TIMEOUT_MS;
		this.maxConcurrentDeliveries = opts.maxConcurrentDeliveries
			?? MAX_CONCURRENT_DELIVERIES;
		this.maxQueuedDeliveries = opts.maxQueuedDeliveries ?? MAX_QUEUED_DELIVERIES;
		this.webSocketFactory = opts.webSocketFactory
			?? ((url, options) => new WebSocket(url, options));
	}

	start(registration: SlackSocketRegistration): void {
		const expectedApplicationId = registration.applicationId?.trim() || null;
		const preferredKey = expectedApplicationId
			? `app:${expectedApplicationId}`
			: `registration:${registration.registrationId}`;
		let conn = this.connections.get(preferredKey);
		if (!conn) {
			conn = [...this.connections.values()].find((candidate) =>
				candidate.registrationId === registration.registrationId
				|| Boolean(expectedApplicationId && (
					candidate.applicationId === expectedApplicationId
					|| candidate.expectedApplicationId === expectedApplicationId
				)),
			);
		}

		if (conn && conn.appToken === registration.appToken
			&& conn.expectedApplicationId === expectedApplicationId
			&& conn.state !== "fatal") {
			return;
		}

		if (conn) {
			this.restartConnection(conn, preferredKey, registration, expectedApplicationId);
		} else {
			conn = this.createConnection(preferredKey, registration, expectedApplicationId);
			this.connections.set(preferredKey, conn);
		}

		if (this.apiUrlIsOverride && !isLoopbackBind(this.bindAddress)) {
			this.fatal(conn, "Slack API override requires a loopback event-server bind");
			return;
		}
		void this.connect(conn);
	}

	stop(id: string): void {
		const conn = this.connections.get(`app:${id}`)
			?? this.connections.get(`registration:${id}`)
			?? [...this.connections.values()].find((candidate) =>
				candidate.applicationId === id || candidate.registrationId === id,
			);
		if (!conn) return;
		this.teardown(conn);
		this.connections.delete(conn.key);
	}

	stopAll(): void {
		for (const conn of [...this.connections.values()]) {
			this.teardown(conn);
			this.connections.delete(conn.key);
		}
	}

	health(): SlackConnectionHealth[] {
		return [...this.connections.values()].map((conn) => ({
			application_id: this.publicApplicationId(conn),
			state: conn.state,
			last_event_at: conn.lastEventAt,
			delivered_event_count: conn.deliveredEventCount,
			connect_count: conn.connectCount,
			reconnect_count: conn.reconnectCount,
			...(conn.fatalReason ? { fatal_reason: conn.fatalReason } : {}),
		}));
	}

	private createConnection(
		key: string,
		registration: SlackSocketRegistration,
		expectedApplicationId: string | null,
	): Connection {
		return {
			key,
			registrationId: registration.registrationId,
			expectedApplicationId,
			applicationId: null,
			appToken: registration.appToken,
			state: "connecting",
			session: null,
			ws: null,
			acknowledgedEnvelopes: new AcknowledgedEnvelopeCache(),
			handshakeTimer: null,
			stalenessTimer: null,
			reconnectTimer: null,
			backoffAttempt: 0,
			lastEventAt: null,
			deliveredEventCount: 0,
			connectCount: 0,
			reconnectCount: 0,
			fatalReason: "",
			generation: 1,
			pendingAcknowledgements: new Set(),
		};
	}

	private restartConnection(
		conn: Connection,
		preferredKey: string,
		registration: SlackSocketRegistration,
		expectedApplicationId: string | null,
	): void {
		this.teardown(conn);
		if (conn.key !== preferredKey) {
			this.connections.delete(conn.key);
			conn.key = preferredKey;
			this.connections.set(preferredKey, conn);
		}
		conn.registrationId = registration.registrationId;
		conn.expectedApplicationId = expectedApplicationId;
		conn.applicationId = null;
		conn.appToken = registration.appToken;
		conn.state = "connecting";
		conn.backoffAttempt = 0;
		conn.fatalReason = "";
	}

	private async fetchConnectionUrl(
		appToken: string,
	): Promise<
		{ kind: "success"; url: string }
		| Extract<SlackBootstrapClassification, { kind: "retry" | "fatal" }>
	> {
		const controller = new AbortController();
		const timeout = scheduleUnrefTimeout(() => controller.abort(), this.restTimeoutMs);
		try {
			const response = await fetch(`${slackApiUrl()}apps.connections.open`, {
				method: "POST",
				headers: {
					Authorization: `Bearer ${appToken}`,
					"Content-Type": "application/x-www-form-urlencoded",
				},
				body: "",
				redirect: "error",
				signal: controller.signal,
			});
			let body: unknown = null;
			try {
				body = await response.json();
			} catch {
				// A malformed transient response is retried below.
			}
			const policy = classifySlackBootstrapResponse(
				response.status,
				body,
				response.headers.get("Retry-After"),
			);
			if (policy.kind !== "success") return policy;
			const url = record(body)?.url;
			if (typeof url !== "string" || !validateSlackSocketUrl(url)) {
				return {
					kind: "fatal",
					reason: "apps.connections.open returned an untrusted WebSocket URL",
				};
			}
			return { kind: "success", url };
		} finally {
			clearScheduledTimeout(timeout);
		}
	}

	private async connect(conn: Connection): Promise<void> {
		const generation = conn.generation;
		conn.state = "connecting";
		let bootstrap: Awaited<ReturnType<SlackSocketManager["fetchConnectionUrl"]>>;
		try {
			bootstrap = await this.fetchConnectionUrl(conn.appToken);
		} catch {
			if (!isCurrentGeneration(conn, generation)) return;
			console.error(`slack socket: apps.connections.open failed for ${this.label(conn)}`);
			this.scheduleReconnect(conn);
			return;
		}
		if (!isCurrentGeneration(conn, generation)) return;
		if (bootstrap.kind === "fatal") {
			this.fatal(conn, bootstrap.reason);
			return;
		}
		if (bootstrap.kind === "retry") {
			this.scheduleReconnect(conn, bootstrap.retryAfterMs);
			return;
		}

		const session = new SlackSocketSession({
			acknowledgedEnvelopes: conn.acknowledgedEnvelopes,
		});
		conn.session = session;
		if (conn.connectCount > 0) conn.reconnectCount++;
		conn.connectCount++;

		let ws: WebSocket;
		try {
			ws = this.webSocketFactory(bootstrap.url, { maxPayload: MAX_PAYLOAD_BYTES });
		} catch {
			console.error(`slack socket: WebSocket construction failed for ${this.label(conn)}`);
			this.scheduleReconnect(conn);
			return;
		}
		conn.ws = ws;
		conn.handshakeTimer = scheduleUnrefTimeout(() => {
			if (!isCurrentGeneration(conn, generation) || conn.ws !== ws) return;
			console.error(`slack socket: handshake timed out for ${this.label(conn)}`);
			this.scheduleReconnect(conn);
		}, this.handshakeTimeoutMs);

		ws.on("message", (data) => {
			if (!isCurrentGeneration(conn, generation) || conn.ws !== ws) return;
			if (conn.state === "connected") this.resetStalenessWatchdog(conn, generation);
			this.apply(conn, generation, session, session.onFrame(String(data)));
		});
		ws.on("ping", () => {
			if (!isCurrentGeneration(conn, generation) || conn.ws !== ws) return;
			if (conn.state === "connected") this.resetStalenessWatchdog(conn, generation);
		});
		ws.on("close", () => {
			if (!isCurrentGeneration(conn, generation) || conn.ws !== ws) return;
			this.apply(conn, generation, session, session.onSocketClose());
		});
		ws.on("error", () => {
			// The close event drives recovery. Never stringify the error: `ws`
			// errors may contain the credential-bearing one-use connection URL.
			console.error(`slack socket: WebSocket error for ${this.label(conn)}`);
		});
	}

	private apply(
		conn: Connection,
		generation: number,
		session: SlackSocketSession,
		actions: SlackSocketAction[],
	): void {
		for (let index = 0; index < actions.length; index++) {
			if (!isCurrentGeneration(conn, generation)) return;
			const action = actions[index];
			switch (action.kind) {
				case "send": {
					const next = actions[index + 1];
					const delivery = next?.kind === "deliver"
						&& next.envelopeId === action.ackEnvelopeId ? next : null;
					if (delivery) index++;
					this.acknowledge(conn, generation, session, action, delivery);
					break;
				}

				case "deliver":
					// The protocol always pairs delivery immediately after its ack.
					console.error(`slack socket: unpaired delivery for ${this.label(conn)}`);
					this.scheduleReconnect(conn);
					return;

				case "connected":
					this.connected(conn, generation, action.applicationId);
					break;

				case "reconnect":
					this.scheduleReconnect(conn);
					return;

				case "fatal":
					this.fatal(conn, action.reason);
					return;
			}
		}
	}

	private connected(conn: Connection, generation: number, applicationId: string): void {
		if (conn.expectedApplicationId && conn.expectedApplicationId !== applicationId) {
			this.fatal(conn, "Socket Mode hello application id did not match registration");
			return;
		}
		const targetKey = `app:${applicationId}`;
		const conflict = this.connections.get(targetKey);
		if (conflict && conflict !== conn) {
			this.fatal(conn, "another Socket Mode connection already owns this application id");
			return;
		}
		if (conn.key !== targetKey) {
			this.connections.delete(conn.key);
			conn.key = targetKey;
			this.connections.set(targetKey, conn);
		}
		conn.applicationId = applicationId;
		conn.state = "connected";
		conn.backoffAttempt = 0;
		conn.fatalReason = "";
		conn.handshakeTimer = clearScheduledTimeout(conn.handshakeTimer);
		this.resetStalenessWatchdog(conn, generation);
	}

	private acknowledge(
		conn: Connection,
		generation: number,
		session: SlackSocketSession,
		action: Extract<SlackSocketAction, { kind: "send" }>,
		delivery: Extract<SlackSocketAction, { kind: "deliver" }> | null,
	): void {
		if (delivery && !this.reserveDelivery()) {
			console.error(`slack socket: delivery queue full for ${this.label(conn)}; reconnecting`);
			this.scheduleReconnect(conn);
			return;
		}

		const ws = conn.ws;
		if (!ws || ws.readyState !== WebSocket.OPEN) {
			if (delivery) this.releaseDeliveryReservation();
			this.scheduleReconnect(conn);
			return;
		}

		let settled = false;
		const settle = (error?: Error): void => {
			if (settled) return;
			settled = true;
			conn.pendingAcknowledgements.delete(pending);
			if (error) {
				if (delivery) this.releaseDeliveryReservation();
				if (isCurrentGeneration(conn, generation)) {
					console.error(`slack socket: acknowledgement failed for ${this.label(conn)}`);
					this.scheduleReconnect(conn);
				}
				return;
			}

			session.onAcknowledged(action.ackEnvelopeId);
			if (delivery) {
				this.acceptDelivery({
					conn,
					generation,
					envelopeId: delivery.envelopeId,
					payload: delivery.payload,
				});
			}
		};
		const pending: PendingAcknowledgement = {
			fail: () => settle(new Error("socket closed before acknowledgement completed")),
		};
		conn.pendingAcknowledgements.add(pending);
		try {
			ws.send(action.frame, (error) => settle(error));
		} catch (error) {
			settle(error instanceof Error ? error : new Error("acknowledgement send failed"));
		}
	}

	private reserveDelivery(): boolean {
		const capacity = this.maxConcurrentDeliveries + this.maxQueuedDeliveries;
		if (this.activeDeliveries + this.deliveryQueue.length + this.reservedDeliveries
			>= capacity) {
			return false;
		}
		this.reservedDeliveries++;
		return true;
	}

	private releaseDeliveryReservation(): void {
		this.reservedDeliveries = Math.max(0, this.reservedDeliveries - 1);
	}

	private acceptDelivery(delivery: AcceptedDelivery): void {
		this.releaseDeliveryReservation();
		if (this.activeDeliveries < this.maxConcurrentDeliveries) {
			this.startDelivery(delivery);
		} else {
			this.deliveryQueue.push(delivery);
		}
	}

	private startDelivery(delivery: AcceptedDelivery): void {
		this.activeDeliveries++;
		void handleSlackWebhook(
			this.storage,
			JSON.stringify(delivery.payload),
			delivery.payload,
		).then((result) => {
			if (this.wasDelivered(result)
				&& isCurrentGeneration(delivery.conn, delivery.generation)
				&& this.connections.get(delivery.conn.key) === delivery.conn) {
				delivery.conn.deliveredEventCount++;
				delivery.conn.lastEventAt = new Date().toISOString();
			}
		}).catch(() => {
			console.error(`slack socket: delivery failed for envelope `
				+ `${this.logIdentifier(delivery.envelopeId, delivery.conn.appToken)} `
				+ `on ${this.label(delivery.conn)}`);
		}).finally(() => {
			this.activeDeliveries--;
			const next = this.deliveryQueue.shift();
			if (next) this.startDelivery(next);
		});
	}

	private wasDelivered(result: HandlerResult): boolean {
		return record(result.body)?.delivered_to !== undefined;
	}

	private resetStalenessWatchdog(conn: Connection, generation: number): void {
		conn.stalenessTimer = clearScheduledTimeout(conn.stalenessTimer);
		conn.stalenessTimer = scheduleUnrefTimeout(() => {
			if (!isCurrentGeneration(conn, generation) || !conn.session) return;
			this.apply(conn, generation, conn.session, conn.session.onTimer("staleness"));
		}, this.stalenessTimeoutMs);
	}

	private scheduleReconnect(conn: Connection, retryAfter = 0): void {
		conn.generation++;
		const generation = conn.generation;
		this.closeSocket(conn);
		conn.session = null;
		conn.state = "backoff";
		const delay = calculateBackoffDelay({
			attempt: conn.backoffAttempt,
			baseMs: BACKOFF_BASE_MS,
			maxMs: BACKOFF_MAX_MS,
			minimumMs: Math.max(BACKOFF_BASE_MS, retryAfter),
			random: this.random,
		});
		conn.backoffAttempt = Math.min(conn.backoffAttempt + 1, 10);
		conn.reconnectTimer = scheduleUnrefTimeout(() => {
			if (!isCurrentGeneration(conn, generation)) return;
			void this.connect(conn);
		}, delay);
	}

	private fatal(conn: Connection, reason: string): void {
		conn.generation++;
		this.closeSocket(conn);
		conn.session = null;
		conn.state = "fatal";
		conn.fatalReason = reason;
		console.error(`slack socket: FATAL for ${this.label(conn)}: ${reason}; not reconnecting`);
	}

	private teardown(conn: Connection): void {
		conn.generation++;
		this.closeSocket(conn);
		conn.session = null;
	}

	private closeSocket(conn: Connection): void {
		conn.handshakeTimer = clearScheduledTimeout(conn.handshakeTimer);
		conn.stalenessTimer = clearScheduledTimeout(conn.stalenessTimer);
		conn.reconnectTimer = clearScheduledTimeout(conn.reconnectTimer);
		for (const pending of [...conn.pendingAcknowledgements]) pending.fail();
		const ws = conn.ws;
		conn.ws = null;
		if (ws) disposeWebSocket(ws, 4000, "reconnecting");
	}

	private label(conn: Connection): string {
		const identifier = conn.applicationId ?? conn.expectedApplicationId ?? conn.registrationId;
		return `app ${this.logIdentifier(identifier, conn.appToken)}`;
	}

	private publicApplicationId(conn: Connection): string | null {
		const identifier = conn.applicationId ?? conn.expectedApplicationId;
		return identifier && (!conn.appToken || !identifier.includes(conn.appToken))
			? identifier
			: null;
	}

	private logIdentifier(identifier: string, secret: string): string {
		if (!identifier || (secret && identifier.includes(secret))) return "[redacted]";
		return identifier.replace(/[^A-Za-z0-9_.:-]/g, "?").slice(0, 128);
	}
}
