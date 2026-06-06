import { describe, it, expect } from "vitest";
import {
	createTopicEvent,
	normalizeGitHubPayload,
	normalizeLinearPayload,
	normalizeSlackPayload,
	subscriptionKeysForEvent,
} from "../src/core";

describe("normalizeGitHubPayload", () => {
	it("normalizes an issue event", () => {
		const event = normalizeGitHubPayload("issues", "delivery-1", {
			action: "opened",
			repository: { full_name: "org/repo" },
			installation: { id: 42 },
		});
		expect(event).not.toBeNull();
		expect(event!.source).toBe("github");
		expect(event!.type).toBe("github.issues");
		expect(event!.repo).toBe("org/repo");
		expect(event!.installation_id).toBe(42);
		expect(event!.id).toBe("delivery-1");
	});

	it("returns null when no repository", () => {
		const event = normalizeGitHubPayload("push", "d-1", { action: "opened" });
		expect(event).toBeNull();
	});

	it("generates an id when delivery is empty", () => {
		const event = normalizeGitHubPayload("push", "", {
			repository: { full_name: "org/repo" },
		});
		expect(event!.id).toBeTruthy();
	});
});

describe("normalizeLinearPayload", () => {
	it("normalizes an issue update", () => {
		const event = normalizeLinearPayload({
			action: "update",
			type: "Issue",
			data: {
				id: "abc",
				identifier: "PROJ-1",
				team: { key: "PROJ" },
			},
		});
		expect(event.source).toBe("linear");
		expect(event.type).toBe("linear.Issue.update");
		expect(event.team_key).toBe("PROJ");
	});

	it("handles missing team", () => {
		const event = normalizeLinearPayload({
			action: "create",
			type: "Comment",
			data: {},
		});
		expect(event.type).toBe("linear.Comment.create");
		expect(event.team_key).toBeUndefined();
	});
});

describe("normalizeSlackPayload", () => {
	it("handles url_verification", () => {
		const result = normalizeSlackPayload({
			type: "url_verification",
			challenge: "abc123",
		});
		expect(result.skip).toBe(true);
		expect(result.challenge).toBe("abc123");
		expect(result.event).toBeNull();
	});

	it("normalizes app_mention", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event_id: "Ev01",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "<@U99> hello",
				ts: "1234.5678",
			},
		});
		expect(result.skip).toBe(false);
		expect(result.event!.type).toBe("slack.mention");
		expect(result.event!.workspace).toBe("T123");
		expect(result.event!.channel).toBe("C456");
		expect(result.event!.payload).toMatchObject({
			user_id: "U123",
			channel: "C456",
			text: "<@U99> hello",
		});
	});

	it("normalizes DM", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "message",
				user: "U123",
				channel: "D789",
				channel_type: "im",
				text: "hello",
				ts: "123",
			},
		});
		expect(result.event!.type).toBe("slack.dm");
	});

	it("normalizes thread reply", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "message",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "reply",
				ts: "123.456",
				thread_ts: "123.000",
			},
		});
		expect(result.event!.type).toBe("slack.thread_reply");
	});

	it("skips bot messages", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention",
				user: "U123",
				bot_id: "B123",
				channel: "C456",
				channel_type: "channel",
				text: "bot",
				ts: "123",
			},
		});
		expect(result.skip).toBe(true);
		expect(result.event).toBeNull();
	});

	it("skips non-threaded channel messages", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "message",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "hello",
				ts: "123",
			},
		});
		expect(result.skip).toBe(true);
	});

	it("skips non-event_callback types", () => {
		const result = normalizeSlackPayload({ type: "app_rate_limited" });
		expect(result.skip).toBe(true);
	});

	it("truncates long text to 4000 chars", () => {
		const longText = "a".repeat(5000);
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: longText,
				ts: "123",
			},
		});
		expect((result.event!.payload as Record<string, string>).text.length).toBe(4000);
	});
});

describe("subscriptionKeysForEvent", () => {
	it("returns repo key for github events", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "github",
			type: "github.issues",
			repo: "org/repo",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual(["org/repo"]);
	});

	it("returns linear key for linear events", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "linear",
			type: "linear.Issue.update",
			team_key: "PROJ",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual(["linear:PROJ"]);
	});

	it("returns workspace and channel keys for slack events", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "slack",
			type: "slack.mention",
			workspace: "T123",
			channel: "C456",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual(["slack:T123", "slack:T123:C456"]);
	});

	it("returns only workspace key when no channel", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "slack",
			type: "slack.dm",
			workspace: "T123",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual(["slack:T123"]);
	});

	it("returns empty array when no routing fields", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "unknown",
			type: "test",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual([]);
	});
});

describe("createTopicEvent", () => {
	it("creates event with topic as type", () => {
		const event = createTopicEvent("my-topic", {
			source: "my-app",
			payload: { key: "value" },
		});
		expect(event.type).toBe("my-topic");
		expect(event.source).toBe("my-app");
		expect(event.payload).toEqual({ key: "value" });
		expect(event.id).toBeTruthy();
		expect(event.timestamp).toBeTruthy();
	});

	it("uses body as payload when no payload field", () => {
		const event = createTopicEvent("test", { foo: "bar" });
		expect(event.payload).toEqual({ foo: "bar" });
	});

	it("preserves routing fields from body", () => {
		const event = createTopicEvent("deploy.complete", {
			repo: "org/repo",
			workspace: "T123",
			channel: "C456",
			payload: { status: "success" },
		});
		expect(event.repo).toBe("org/repo");
		expect(event.workspace).toBe("T123");
		expect(event.channel).toBe("C456");
	});

	it("uses provided id when present", () => {
		const event = createTopicEvent("test", { id: "custom-id" });
		expect(event.id).toBe("custom-id");
	});

	it("defaults source to custom", () => {
		const event = createTopicEvent("test", {});
		expect(event.source).toBe("custom");
	});
});
