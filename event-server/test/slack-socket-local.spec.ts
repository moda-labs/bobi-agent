import { EventEmitter } from "node:events";
import WebSocket from "ws";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NormalizedEvent, StorageAdapter } from "@moda-labs/bobi-events-core";
import {
	classifySlackBootstrapResponse,
	isLoopbackBind,
	SlackSocketManager,
	validateSlackSocketUrl,
} from "../src/slack-socket-local";

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	vi.useRealTimers();
});

describe("Slack Socket Mode bootstrap policy", () => {
	it("accepts only credential-free wss URLs", () => {
		expect(validateSlackSocketUrl("wss://wss-primary.slack.com/link/?ticket=one")).toBe(true);
		for (const value of [
			"ws://wss-primary.slack.com/link",
			"https://wss-primary.slack.com/link",
			"wss://user:pass@wss-primary.slack.com/link",
			"not a url",
		]) {
			expect(validateSlackSocketUrl(value)).toBe(false);
		}
	});

	it("recognizes only loopback listener addresses for API overrides", () => {
		for (const value of ["127.0.0.1", "127.1.2.3", "::1", "localhost"]) {
			expect(isLoopbackBind(value)).toBe(true);
		}
		for (const value of ["0.0.0.0", "::", "10.0.0.5", "example.test", "127.evil.test"]) {
			expect(isLoopbackBind(value)).toBe(false);
		}
	});

	it("classifies successful bootstrap without retaining the one-use URL", () => {
		expect(classifySlackBootstrapResponse(200, {
			ok: true,
			url: "wss://wss-primary.slack.com/link/?ticket=one",
		}, null)).toEqual({ kind: "success" });
	});

	it("honors Retry-After for HTTP and Slack body rate limits", () => {
		expect(classifySlackBootstrapResponse(429, {}, "12")).toEqual({
			kind: "retry", retryAfterMs: 12_000,
		});
		expect(classifySlackBootstrapResponse(200, {
			ok: false, error: "rate_limited",
		}, "3")).toEqual({ kind: "retry", retryAfterMs: 3_000 });
	});

	it("parks credential and scope failures but retries transient failures", () => {
		for (const [status, body] of [
			[401, {}],
			[403, {}],
			[200, { ok: false, error: "invalid_auth" }],
			[200, { ok: false, error: "token_revoked" }],
			[200, { ok: false, error: "missing_scope" }],
		] as const) {
			expect(classifySlackBootstrapResponse(status, body, null).kind).toBe("fatal");
		}
		for (const [status, body] of [
			[500, {}],
			[503, {}],
			[200, { ok: false, error: "internal_error" }],
			[200, { ok: true }],
		] as const) {
			expect(classifySlackBootstrapResponse(status, body, null).kind).toBe("retry");
		}
	});
});

class FakeSocket extends EventEmitter {
	readyState = WebSocket.OPEN;
	readonly sent: string[] = [];
	sendError: Error | undefined;

	send(frame: string, callback?: (error?: Error) => void): void {
		this.sent.push(frame);
		callback?.(this.sendError);
	}

	close(): void {
		this.readyState = WebSocket.CLOSED;
	}

	terminate(): void {
		this.readyState = WebSocket.CLOSED;
	}
}

function slackPayload(eventId: string): Record<string, unknown> {
	return {
		type: "event_callback",
		team_id: "T1",
		api_app_id: "A1",
		event_id: eventId,
		event: {
			type: "message",
			user: "U1",
			channel: "D1",
			channel_type: "im",
			text: eventId,
			ts: "1.000",
		},
	};
}

function socketEnvelope(envelopeId: string): string {
	return JSON.stringify({
		type: "events_api",
		envelope_id: envelopeId,
		payload: slackPayload(`Ev-${envelopeId}`),
	});
}

function managerHarness(
	deliver: (event: NormalizedEvent) => Promise<string[]> = async () => ["dep-1"],
	managerOptions: {
		maxConcurrentDeliveries?: number;
		maxQueuedDeliveries?: number;
		handshakeTimeoutMs?: number;
		stalenessTimeoutMs?: number;
	} = {},
) {
	const sockets: FakeSocket[] = [];
	const factory = vi.fn(() => {
		const socket = new FakeSocket();
		sockets.push(socket);
		return socket as unknown as WebSocket;
	});
	const fetchStub = vi.fn(async () => new Response(JSON.stringify({
		ok: true,
		url: `wss://socket.test/${sockets.length + 1}`,
	}), {
		status: 200,
		headers: { "Content-Type": "application/json" },
	}));
	vi.stubGlobal("fetch", fetchStub);
	const storage = {
		getSlackWorkspace: async () => ({
			bots: { A1: { bot_token: "xoxb", bot_id: "B1", app_id: "A1" } },
		}),
		deliver,
	} as unknown as StorageAdapter;
	const manager = new SlackSocketManager(storage, {
		webSocketFactory: factory,
		...managerOptions,
	});
	manager.start({ registrationId: "T1:A1", applicationId: "A1", appToken: "secret" });
	return { manager, sockets, factory, fetchStub };
}

async function connectHarness(harness: ReturnType<typeof managerHarness>): Promise<FakeSocket> {
	await vi.waitFor(() => expect(harness.sockets).toHaveLength(1));
	const socket = harness.sockets[0];
	socket.emit("message", JSON.stringify({
		type: "hello",
		connection_info: { app_id: "A1" },
	}));
	expect(harness.manager.health()[0].state).toBe("connected");
	return socket;
}

describe("SlackSocketManager", () => {
	it("reuses a hello-rekeyed provisional connection on re-registration", async () => {
		const sockets: FakeSocket[] = [];
		const fetchStub = vi.fn(async () => new Response(JSON.stringify({
			ok: true, url: `wss://socket.test/${sockets.length + 1}`,
		}), { status: 200, headers: { "Content-Type": "application/json" } }));
		vi.stubGlobal("fetch", fetchStub);
		const manager = new SlackSocketManager({} as StorageAdapter, {
			webSocketFactory: () => {
				const socket = new FakeSocket();
				sockets.push(socket);
				return socket as unknown as WebSocket;
			},
		});
		const registration = { registrationId: "T1:B1", appToken: "secret" };
		manager.start(registration);
		await vi.waitFor(() => expect(sockets).toHaveLength(1));
		sockets[0].emit("message", JSON.stringify({
			type: "hello", connection_info: { app_id: "A1" },
		}));

		manager.start(registration);
		await new Promise((resolve) => setTimeout(resolve, 25));

		expect(sockets).toHaveLength(1);
		expect(fetchStub).toHaveBeenCalledOnce();
		expect(manager.health()).toHaveLength(1);
		manager.stopAll();
	});

	it("queues the wire acknowledgement before entering the existing delivery path", async () => {
		const order: string[] = [];
		const deliver = vi.fn(async () => {
			order.push("deliver");
			return ["dep-1"];
		});
		const harness = managerHarness(deliver);
		const socket = await connectHarness(harness);
		const originalSend = socket.send.bind(socket);
		socket.send = (frame, callback) => {
			order.push("ack");
			originalSend(frame, callback);
		};

		socket.emit("message", socketEnvelope("one"));
		await vi.waitFor(() => expect(deliver).toHaveBeenCalledOnce());

		expect(order).toEqual(["ack", "deliver"]);
		expect(socket.sent).toEqual(['{"envelope_id":"one"}']);
		expect(harness.factory).toHaveBeenCalledWith(
			expect.stringMatching(/^wss:\/\//),
			{ maxPayload: 1_048_576 },
		);
		expect(harness.fetchStub).toHaveBeenCalledWith(
			expect.stringMatching(/apps\.connections\.open$/),
			expect.objectContaining({ redirect: "error" }),
		);
		harness.manager.stopAll();
	});

	it("does not cache or deliver when acknowledgement completion fails", async () => {
		const errors: string[] = [];
		vi.spyOn(console, "error").mockImplementation((...args) => {
			errors.push(args.join(" "));
		});
		const deliver = vi.fn(async () => ["dep-1"]);
		const harness = managerHarness(deliver);
		const socket = await connectHarness(harness);
		socket.sendError = new Error("stub send failure");

		socket.emit("message", socketEnvelope("failed"));

		expect(deliver).not.toHaveBeenCalled();
		expect(harness.manager.health()[0].state).toBe("backoff");
		expect(errors.join(" ")).not.toContain("secret");
		expect(JSON.stringify(harness.manager.health())).not.toContain("secret");
		harness.manager.stopAll();
	});

	it("recovers when the handshake or connected stream goes stale", async () => {
		vi.spyOn(console, "error").mockImplementation(() => undefined);
		const handshake = managerHarness(undefined, { handshakeTimeoutMs: 10 });
		await vi.waitFor(() => expect(handshake.sockets).toHaveLength(1));
		await vi.waitFor(() => expect(handshake.manager.health()[0].state).toBe("backoff"), {
			timeout: 500,
		});
		handshake.manager.stopAll();

		const stale = managerHarness(undefined, { stalenessTimeoutMs: 10 });
		await connectHarness(stale);
		await vi.waitFor(() => expect(stale.manager.health()[0].state).toBe("backoff"), {
			timeout: 500,
		});
		stale.manager.stopAll();
	});

	it("bounds accepted delivery work and keeps already-acked work across reconnect", async () => {
		const resolvers: Array<(value: string[]) => void> = [];
		const deliver = vi.fn(() => new Promise<string[]>((resolve) => resolvers.push(resolve)));
		const harness = managerHarness(deliver, {
			maxConcurrentDeliveries: 1,
			maxQueuedDeliveries: 1,
		});
		const socket = await connectHarness(harness);

		socket.emit("message", socketEnvelope("one"));
		socket.emit("message", socketEnvelope("two"));
		socket.emit("message", socketEnvelope("overflow"));
		await vi.waitFor(() => expect(deliver).toHaveBeenCalledTimes(1));

		expect(socket.sent).toEqual([
			'{"envelope_id":"one"}',
			'{"envelope_id":"two"}',
		]);
		expect(harness.manager.health()[0].state).toBe("backoff");
		resolvers.shift()?.(["dep-1"]);
		await vi.waitFor(() => expect(deliver).toHaveBeenCalledTimes(2));
		resolvers.shift()?.(["dep-1"]);
		harness.manager.stopAll();
	});

	it("parks an application-id mismatch as fatal", async () => {
		const harness = managerHarness();
		await vi.waitFor(() => expect(harness.sockets).toHaveLength(1));
		harness.sockets[0].emit("message", JSON.stringify({
			type: "hello", connection_info: { app_id: "A_OTHER" },
		}));

		expect(harness.manager.health()[0]).toMatchObject({
			application_id: "A1",
			state: "fatal",
		});
		harness.manager.stopAll();
	});

	it("refuses a non-loopback API override before transmitting the app token", () => {
		const errors: string[] = [];
		vi.spyOn(console, "error").mockImplementation((...args) => {
			errors.push(args.join(" "));
		});
		const fetchStub = vi.fn();
		vi.stubGlobal("fetch", fetchStub);
		const manager = new SlackSocketManager({} as StorageAdapter, {
			apiUrlIsOverride: true,
			bindAddress: "0.0.0.0",
		});
		manager.start({ registrationId: "secret", applicationId: "secret", appToken: "secret" });

		expect(fetchStub).not.toHaveBeenCalled();
		expect(manager.health()[0]).toMatchObject({ state: "fatal" });
		expect(JSON.stringify(manager.health())).not.toContain("secret");
		expect(errors.join(" ")).not.toContain("secret");
		manager.stopAll();
	});
});
