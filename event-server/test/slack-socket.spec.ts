import { describe, expect, it } from "vitest";
import {
	AcknowledgedEnvelopeCache,
	SlackSocketSession,
	type SlackSocketAction,
} from "@moda-labs/bobi-events-core/gateway/slack-socket";

const APP_ID = "A0123456789";

function session(
	acknowledgedEnvelopes = new AcknowledgedEnvelopeCache(),
): SlackSocketSession {
	return new SlackSocketSession({ acknowledgedEnvelopes });
}

function frame(value: unknown): string {
	return JSON.stringify(value);
}

function eventsEnvelope(
	envelopeId: string,
	payload: Record<string, unknown>,
	extra: Record<string, unknown> = {},
): string {
	return frame({
		type: "events_api",
		envelope_id: envelopeId,
		accepts_response_payload: false,
		payload,
		...extra,
	});
}

function ack(envelopeId: string): Extract<SlackSocketAction, { kind: "send" }> {
	return {
		kind: "send",
		frame: `{"envelope_id":"${envelopeId}"}`,
		ackEnvelopeId: envelopeId,
	};
}

function eventPayload(eventId: string): Record<string, unknown> {
	return {
		token: "verification-token",
		team_id: "T0123456789",
		api_app_id: APP_ID,
		type: "event_callback",
		event_id: eventId,
		event_time: 1_753_200_000,
		authorizations: [{ team_id: "T0123456789", user_id: "U_BOT", is_bot: true }],
		event: {
			type: "app_mention",
			user: "U_USER",
			text: "<@U_BOT> deploy please",
			channel: "C0123456789",
			ts: "1753200000.000100",
			blocks: [{ type: "section", fields: [{ type: "mrkdwn", text: "*exact*" }] }],
		},
	};
}

describe("SlackSocketSession frames", () => {
	it("signals connected with the app id from hello", () => {
		const actions = session().onFrame(frame({
			type: "hello",
			num_connections: 1,
			connection_info: { app_id: APP_ID },
			debug_info: { host: "applink-1" },
		}));

		expect(actions).toEqual([{ kind: "connected", applicationId: APP_ID }]);
	});

	it("emits the exact acknowledgement before the unchanged event payload", () => {
		const s = session();
		const payload = eventPayload("Ev-api-1");

		const actions = s.onFrame(eventsEnvelope("env-1", payload, {
			retry_attempt: 0,
			retry_reason: "",
		}));

		expect(actions).toEqual([
			ack("env-1"),
			{ kind: "deliver", envelopeId: "env-1", payload },
		]);
		expect((actions[0] as { frame: string }).frame).toBe("{\"envelope_id\":\"env-1\"}");
	});

	it("ignores malformed frames and remains usable", () => {
		const s = session();
		const malformed = [
			"not json {",
			frame(null),
			frame([]),
			frame("hello"),
			frame(42),
			frame({}),
			frame({ type: "hello" }),
			frame({ type: "hello", connection_info: null }),
			frame({ type: "hello", connection_info: { app_id: "" } }),
			frame({ type: "hello", connection_info: { app_id: 123 } }),
			frame({ type: "events_api", payload: eventPayload("Ev-no-id") }),
			frame({ type: "events_api", envelope_id: "", payload: eventPayload("Ev-empty-id") }),
			frame({ type: "events_api", envelope_id: 123, payload: eventPayload("Ev-bad-id") }),
		];

		for (const raw of malformed) {
			expect(s.onFrame(raw)).toEqual([]);
		}
		expect(s.onFrame(frame({
			type: "hello",
			connection_info: { app_id: APP_ID },
		}))).toEqual([{ kind: "connected", applicationId: APP_ID }]);
	});

	it("acknowledges and drops malformed event payloads", () => {
		const s = session();
		for (const [index, payload] of [undefined, null, [], "bad payload"].entries()) {
			const envelopeId = `env-malformed-${index}`;
			expect(s.onFrame(frame({
				type: "events_api",
				envelope_id: envelopeId,
				...(payload === undefined ? {} : { payload }),
			}))).toEqual([ack(envelopeId)]);
		}
	});

	it("acknowledges unsupported envelope types without delivering", () => {
		const s = session();
		for (const [index, type] of ["slash_commands", "interactive", "future_type"].entries()) {
			const envelopeId = `env-unsupported-${index}`;
			expect(s.onFrame(frame({
				type,
				envelope_id: envelopeId,
				payload: { must_not: "be delivered" },
			}))).toEqual([ack(envelopeId)]);
		}
		expect(s.onFrame(frame({
			type: "slash_commands",
			payload: { command: "/deploy" },
		}))).toEqual([]);
	});
});

describe("SlackSocketSession reconnect policy", () => {
	it("treats warning disconnects as advisory", () => {
		expect(session().onFrame(frame({
			type: "disconnect",
			reason: "warning",
			debug_info: { host: "applink-1" },
		}))).toEqual([]);
	});

	it("reconnects for refresh requests and unknown disconnect reasons", () => {
		for (const reason of ["refresh_requested", "server_maintenance"]) {
			expect(session().onFrame(frame({ type: "disconnect", reason }))).toEqual([
				{ kind: "reconnect" },
			]);
		}
	});

	it("parks fatal when Socket Mode was disabled", () => {
		const actions = session().onFrame(frame({
			type: "disconnect",
			reason: "link_disabled",
		}));

		expect(actions).toHaveLength(1);
		expect(actions[0]).toMatchObject({ kind: "fatal" });
	});

	it("reconnects on staleness and every socket close", () => {
		const s = session();
		expect(s.onTimer("staleness")).toEqual([{ kind: "reconnect" }]);
		expect(s.onSocketClose()).toEqual([{ kind: "reconnect" }]);
	});
});

describe("SlackSocketSession acknowledged-envelope deduplication", () => {
	it("acks an acknowledged duplicate again but delivers it only once", () => {
		const s = session();
		const raw = eventsEnvelope("env-dedup", eventPayload("Ev-dedup"));

		expect(s.onFrame(raw)).toHaveLength(2);
		s.onAcknowledged("env-dedup");
		expect(s.onFrame(raw)).toEqual([ack("env-dedup")]);
	});

	it("shares acknowledged ids across reconnect-created sessions", () => {
		const acknowledgedEnvelopes = new AcknowledgedEnvelopeCache();
		const firstConnection = session(acknowledgedEnvelopes);
		const raw = eventsEnvelope("env-reconnect", eventPayload("Ev-reconnect"));

		firstConnection.onFrame(raw);
		firstConnection.onAcknowledged("env-reconnect");

		const reconnected = session(acknowledgedEnvelopes);
		expect(reconnected.onFrame(raw)).toEqual([ack("env-reconnect")]);
	});

	it("evicts the least recently acknowledged id at the configured bound", () => {
		const acknowledgedEnvelopes = new AcknowledgedEnvelopeCache(2);
		const s = session(acknowledgedEnvelopes);
		const one = eventsEnvelope("env-1", eventPayload("Ev-1"));
		const two = eventsEnvelope("env-2", eventPayload("Ev-2"));
		const three = eventsEnvelope("env-3", eventPayload("Ev-3"));

		s.onFrame(one);
		s.onAcknowledged("env-1");
		s.onFrame(two);
		s.onAcknowledged("env-2");

		// A successfully re-acked duplicate refreshes its recency.
		expect(s.onFrame(one)).toEqual([ack("env-1")]);
		s.onAcknowledged("env-1");
		s.onFrame(three);
		s.onAcknowledged("env-3");

		expect(s.onFrame(one)).toEqual([ack("env-1")]);
		expect(s.onFrame(two)).toEqual([
			ack("env-2"),
			{ kind: "deliver", envelopeId: "env-2", payload: eventPayload("Ev-2") },
		]);
	});

	it("redelivers after reconnect when the earlier acknowledgement never completed", () => {
		const acknowledgedEnvelopes = new AcknowledgedEnvelopeCache();
		const firstConnection = session(acknowledgedEnvelopes);
		const payload = eventPayload("Ev-unacked");

		expect(firstConnection.onFrame(eventsEnvelope("env-unacked", payload))).toHaveLength(2);
		// Do not call onAcknowledged: the driver did not successfully queue the ack.
		const reconnected = session(acknowledgedEnvelopes);
		expect(reconnected.onFrame(eventsEnvelope("env-unacked", payload, {
			retry_attempt: 1,
			retry_reason: "timeout",
		}))).toEqual([
			ack("env-unacked"),
			{ kind: "deliver", envelopeId: "env-unacked", payload },
		]);

		// Contrast event-server/test/core.spec.ts, "slack: retried event deliveries
		// dedup before the signature check". An HTTP retry proves the webhook was
		// received; Socket Mode retry_attempt may be the first receipt after an
		// unacknowledged reconnect gap, so retry_attempt alone must never suppress it.
	});
});
