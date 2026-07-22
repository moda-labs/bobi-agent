/**
 * Slack Socket Mode protocol core - sans-IO state machine.
 *
 * The local runtime owns REST bootstrap, WebSockets, timers, and delivery.
 * This module consumes one decoded wire frame at a time and returns ordered
 * actions for that runtime to apply. It deliberately records an envelope as
 * acknowledged only when the driver confirms that `ws.send` completed.
 */

export const DEFAULT_ACKNOWLEDGED_ENVELOPE_CAPACITY = 10_000;
export const MAX_SLACK_ENVELOPE_ID_LENGTH = 256;

/** Process-local bounded LRU shared by reconnect-created protocol sessions. */
export class AcknowledgedEnvelopeCache {
	private readonly capacity: number;
	private readonly envelopeIds = new Map<string, true>();

	constructor(capacity = DEFAULT_ACKNOWLEDGED_ENVELOPE_CAPACITY) {
		if (!Number.isInteger(capacity) || capacity <= 0) {
			throw new RangeError("acknowledged envelope capacity must be a positive integer");
		}
		this.capacity = capacity;
	}

	has(envelopeId: string): boolean {
		return this.envelopeIds.has(envelopeId);
	}

	acknowledge(envelopeId: string): void {
		if (!envelopeId) return;
		// Delete + set refreshes recency for acknowledgements of retransmissions.
		this.envelopeIds.delete(envelopeId);
		this.envelopeIds.set(envelopeId, true);
		while (this.envelopeIds.size > this.capacity) {
			const oldest = this.envelopeIds.keys().next().value as string | undefined;
			if (oldest === undefined) break;
			this.envelopeIds.delete(oldest);
		}
	}

	/** Retain acknowledged IDs learned by another connection for the same app. */
	mergeFrom(other: AcknowledgedEnvelopeCache): void {
		if (other === this) return;
		for (const envelopeId of other.envelopeIds.keys()) {
			this.acknowledge(envelopeId);
		}
	}
}

export type SlackSocketAction =
	| { kind: "send"; frame: string; ackEnvelopeId: string }
	| { kind: "deliver"; envelopeId: string; payload: Record<string, unknown> }
	| { kind: "reconnect" }
	| { kind: "connected"; applicationId: string }
	| { kind: "fatal"; reason: string };

export interface SlackSocketSessionOptions {
	acknowledgedEnvelopes?: AcknowledgedEnvelopeCache;
}

function record(value: unknown): Record<string, unknown> | null {
	return value !== null && typeof value === "object" && !Array.isArray(value)
		? value as Record<string, unknown>
		: null;
}

export class SlackSocketSession {
	private readonly acknowledgedEnvelopes: AcknowledgedEnvelopeCache;

	constructor(opts: SlackSocketSessionOptions = {}) {
		this.acknowledgedEnvelopes = opts.acknowledgedEnvelopes
			?? new AcknowledgedEnvelopeCache();
	}

	onFrame(raw: string): SlackSocketAction[] {
		let parsed: unknown;
		try {
			parsed = JSON.parse(raw);
		} catch {
			return [];
		}
		const frame = record(parsed);
		if (!frame) return [];

		if (frame.type === "hello") {
			const applicationId = record(frame.connection_info)?.app_id;
			return typeof applicationId === "string" && applicationId.length > 0
				? [{ kind: "connected", applicationId }]
				: [];
		}

		if (frame.type === "disconnect") {
			if (frame.reason === "warning") return [];
			if (frame.reason === "link_disabled") {
				return [{ kind: "fatal", reason: "socket mode link disabled" }];
			}
			return [{ kind: "reconnect" }];
		}

		const envelopeId = frame.envelope_id;
		if (typeof envelopeId !== "string" || envelopeId.length === 0
			|| envelopeId.length > MAX_SLACK_ENVELOPE_ID_LENGTH) return [];
		const send: SlackSocketAction = {
			kind: "send",
			frame: JSON.stringify({ envelope_id: envelopeId }),
			ackEnvelopeId: envelopeId,
		};
		if (frame.type !== "events_api") return [send];

		const payload = record(frame.payload);
		if (!payload || this.acknowledgedEnvelopes.has(envelopeId)) return [send];
		return [send, { kind: "deliver", envelopeId, payload }];
	}

	onAcknowledged(envelopeId: string): void {
		this.acknowledgedEnvelopes.acknowledge(envelopeId);
	}

	onTimer(kind: "staleness"): SlackSocketAction[] {
		return kind === "staleness" ? [{ kind: "reconnect" }] : [];
	}

	onSocketClose(): SlackSocketAction[] {
		return [{ kind: "reconnect" }];
	}
}
