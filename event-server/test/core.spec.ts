import { describe, it, expect, beforeEach } from "vitest";
import {
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type BubbleAuthContext,
	type SlackWorkspaceRecord,
	type NormalizedEvent,
	namespaceSubKey,
	createTopicEvent,
	normalizeGitHubPayload,
	normalizeLinearPayload,
	normalizeSlackPayload,
	subscriptionKeysForEvent,
	verifyGitHubSignature,
	constantTimeEqual,
	buildBubbleSignature,
	verifyBubbleSignature,
	authenticateDeployment,
	authenticateBubble,
	getAuthRejectionCounters,
	resetAuthRejectionCounters,
	handleGitHubWebhook,
	handleLinearWebhook,
	handleSlackWebhook,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleDeregisterDeployment,
	handleTopicEvent,
	handleSlackSend,
	handleSlackWorkspaceRegister,
	resolveSlackSigningSecret,
} from "../src/core";

describe("normalizeGitHubPayload", () => {
	it("normalizes an issue event with v2 envelope", () => {
		const event = normalizeGitHubPayload("issues", "delivery-1", {
			action: "opened",
			repository: { full_name: "org/repo" },
			installation: { id: 42 },
			sender: { login: "testuser" },
			issue: { number: 10, title: "Bug fix", state: "open",
				html_url: "https://github.com/org/repo/issues/10" },
		});
		expect(event).not.toBeNull();
		expect(event!.v).toBe(2);
		expect(event!.source).toBe("github");
		expect(event!.type).toBe("github.issues");
		expect(event!.topics).toEqual(["github:org/repo"]);
		expect(event!.delivery).toBe("bulk");
		expect(event!.text).toContain("org/repo");
		expect(event!.text).toContain("opened");
		expect(event!.id).toBe("delivery-1");

		// Fields structurally guarantee key data
		expect(event!.fields).toBeDefined();
		expect(event!.fields!.action).toBe("opened");
		expect(event!.fields!.sender).toBe("testuser");
		expect(event!.fields!.number).toBe(10);
		expect(event!.fields!.title).toBe("Bug fix");
		expect(event!.fields!.state).toBe("open");
		expect(event!.fields!.url).toBe("https://github.com/org/repo/issues/10");
	});

	it("extracts assignees from issue payload", () => {
		const event = normalizeGitHubPayload("issues", "d-2", {
			action: "assigned",
			repository: { full_name: "org/repo" },
			sender: { login: "admin" },
			issue: {
				number: 42, title: "Add feature", state: "open",
				assignee: { login: "dev1" },
				assignees: [{ login: "dev1" }, { login: "dev2" }],
			},
		});
		expect(event!.fields!.assignees).toBe("dev1, dev2");
	});

	it("extracts single assignee when assignees array is empty", () => {
		const event = normalizeGitHubPayload("issues", "d-3", {
			action: "assigned",
			repository: { full_name: "org/repo" },
			issue: {
				number: 42, title: "Task", state: "open",
				assignee: { login: "dev1" },
				assignees: [],
			},
		});
		expect(event!.fields!.assignee).toBe("dev1");
	});

	it("extracts comment_id + is_pull_request from an issue_comment on a PR (#411)", () => {
		const event = normalizeGitHubPayload("issue_comment", "d-ic", {
			action: "created",
			repository: { full_name: "org/repo" },
			sender: { login: "modastack" },
			issue: {
				number: 410, title: "spec", state: "open",
				draft: true,
				pull_request: { html_url: "https://github.com/org/repo/pull/410" },
			},
			comment: { id: 99887766, body: "Rendered spec", html_url: "x" },
		});
		expect(event!.type).toBe("github.issue_comment");
		expect(event!.fields!.is_pull_request).toBe(true);
		expect(event!.fields!.comment_id).toBe(99887766);
		expect(event!.fields!.sender).toBe("modastack");
	});

	it("extracts comment_id from a pull_request_review_comment (#411)", () => {
		const event = normalizeGitHubPayload("pull_request_review_comment", "d-prc", {
			action: "created",
			repository: { full_name: "org/repo" },
			sender: { login: "reviewer" },
			pull_request: { number: 12, title: "feat", state: "open" },
			comment: { id: 555, body: "nit", path: "a.ts", html_url: "x" },
		});
		expect(event!.fields!.comment_id).toBe(555);
	});

	it("extracts review_id from a pull_request_review (#411)", () => {
		const event = normalizeGitHubPayload("pull_request_review", "d-pr", {
			action: "submitted",
			repository: { full_name: "org/repo" },
			sender: { login: "reviewer" },
			pull_request: { number: 7, title: "fix", state: "open" },
			review: { id: 4242, state: "changes_requested", body: "redo" },
		});
		expect(event!.fields!.review_id).toBe(4242);
		expect(event!.fields!.review_state).toBe("changes_requested");
	});

	it("omits comment_id when absent (plain issue)", () => {
		const event = normalizeGitHubPayload("issues", "d-plain", {
			action: "opened",
			repository: { full_name: "org/repo" },
			issue: { number: 10, title: "Bug", state: "open" },
		});
		expect(event!.fields!.comment_id).toBeUndefined();
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

	it("drops legacy top-level routing fields (v2 hard cutover)", () => {
		const event = normalizeGitHubPayload("issues", "d-1", {
			action: "opened",
			repository: { full_name: "org/repo" },
			installation: { id: 42 },
		});
		expect(event).not.toBeNull();
		// v2: no top-level repo, installation_id — those live in payload
		expect((event as any).repo).toBeUndefined();
		expect((event as any).installation_id).toBeUndefined();
	});
});

describe("normalizeLinearPayload", () => {
	it("normalizes an issue update with v2 envelope", () => {
		const event = normalizeLinearPayload({
			action: "update",
			type: "Issue",
			data: {
				id: "abc",
				identifier: "PROJ-1",
				title: "Add caching layer",
				team: { key: "PROJ" },
				state: { name: "In Progress" },
			},
		});
		expect(event.v).toBe(2);
		expect(event.source).toBe("linear");
		expect(event.type).toBe("linear.Issue.update");
		expect(event.topics).toEqual(["linear:PROJ"]);
		expect(event.delivery).toBe("bulk");
		expect(event.text).toContain("update");
		expect(event.text).toContain("PROJ-1");
		expect(event.fields!.action).toBe("update");
		expect(event.fields!.identifier).toBe("PROJ-1");
		expect(event.fields!.title).toBe("Add caching layer");
		expect(event.fields!.state).toBe("In Progress");
	});

	it("handles missing team", () => {
		const event = normalizeLinearPayload({
			action: "create",
			type: "Comment",
			data: {},
		});
		expect(event.type).toBe("linear.Comment.create");
		expect(event.topics).toEqual([]);
	});

	it("drops legacy top-level team_key (v2 hard cutover)", () => {
		const event = normalizeLinearPayload({
			action: "update",
			type: "Issue",
			data: { team: { key: "PROJ" } },
		});
		expect((event as any).team_key).toBeUndefined();
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

	it("normalizes app_mention with v2 envelope", () => {
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
		expect(result.event!.v).toBe(2);
		expect(result.event!.type).toBe("slack.mention");
		// workspace topic + channel-scoped topic (for per-channel team routing)
		expect(result.event!.topics).toEqual(["slack:T123", "slack:T123:C456"]);
		expect(result.event!.delivery).toBe("chat");
		expect(result.event!.text).toBe("<@U99> hello");
		expect(result.event!.fields!.user_id).toBe("U123");
		expect(result.event!.fields!.channel).toBe("C456");
		// payload preserved for backward compat
		expect(result.event!.payload).toMatchObject({
			user_id: "U123",
			channel: "C456",
			text: "<@U99> hello",
		});
	});

	it("emits a channel-scoped topic so teams can split one workspace by channel", () => {
		const mk = (channel: string) =>
			normalizeSlackPayload({
				type: "event_callback",
				team_id: "T123",
				event: { type: "app_mention", user: "U1", channel, text: "hi", ts: "1.2" },
			}).event!.topics;
		// a message in C_ENG carries the eng channel topic but not the support one
		expect(mk("C_ENG")).toContain("slack:T123:C_ENG");
		expect(mk("C_ENG")).not.toContain("slack:T123:C_SUPPORT");
		// the workspace-level topic is always present (backward compat)
		expect(mk("C_ENG")).toContain("slack:T123");
	});

	it("normalizes DM with chat delivery", () => {
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
		expect(result.event!.delivery).toBe("chat");
		// a DM is not a real channel — it stays workspace-level only
		expect(result.event!.topics).toEqual(["slack:T123"]);
	});

	it("normalizes group DM (mpim)", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "message",
				user: "U123",
				channel: "G789",
				channel_type: "mpim",
				text: "hello group",
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
		expect(result.event!.fields!.thread_ts).toBe("123.000");
	});

	it("skips own bot messages when selfBotId matches", () => {
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
		}, "B123");
		expect(result.skip).toBe(true);
		expect(result.event).toBeNull();
	});

	it("passes through other bot messages", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention",
				user: "U123",
				bot_id: "B_OTHER",
				channel: "C456",
				channel_type: "channel",
				text: "from another bot",
				ts: "123",
			},
		}, "B_SELF");
		expect(result.skip).toBe(false);
		expect(result.event).not.toBeNull();
		expect(result.event!.type).toBe("slack.mention");
	});

	// Multi-bot workspace (Slack self-spam incident 2026-06-24): the self-filter
	// must accept a SET of our own bot ids, not a single id — two bots can share
	// one workspace, each serving a different team.
	it("skips own bot when bot_id is one of several self ids", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "message",
				user: "U123",
				bot_id: "B2",
				channel: "C456",
				channel_type: "channel",
				thread_ts: "100.000",
				text: "bot two",
				ts: "123",
			},
		}, new Set(["B1", "B2"]));
		expect(result.skip).toBe(true);
		expect(result.event).toBeNull();
	});

	it("passes through a third-party bot not in the self set, preserving bot_id", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "message",
				user: "U123",
				bot_id: "B_OTHER",
				channel: "C456",
				channel_type: "channel",
				thread_ts: "100.000",
				text: "third party",
				ts: "123",
			},
		}, new Set(["B1", "B2"]));
		expect(result.skip).toBe(false);
		expect(result.event).not.toBeNull();
		// bot_id must survive onto the normalized event so the circuit breaker
		// can recognise bot authorship (it reads payload.bot_id).
		expect((result.event!.payload as Record<string, unknown>).bot_id).toBe("B_OTHER");
		expect(result.event!.fields!.bot_id).toBe("B_OTHER");
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
		expect(result.event!.text.length).toBe(4000);
	});

	it("drops legacy top-level workspace/channel (v2 hard cutover)", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "hi",
				ts: "123",
			},
		});
		expect((result.event as any).workspace).toBeUndefined();
		expect((result.event as any).channel).toBeUndefined();
	});
});

describe("verifyGitHubSignature", () => {
	const secret = "test-webhook-secret";

	async function sign(body: string): Promise<string> {
		const key = await crypto.subtle.importKey(
			"raw",
			new TextEncoder().encode(secret),
			{ name: "HMAC", hash: "SHA-256" },
			false,
			["sign"],
		);
		const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
		const hex = Array.from(new Uint8Array(sig))
			.map((b) => b.toString(16).padStart(2, "0"))
			.join("");
		return `sha256=${hex}`;
	}

	it("accepts a valid signature", async () => {
		const body = '{"action":"opened"}';
		const signature = await sign(body);
		const valid = await verifyGitHubSignature(secret, new TextEncoder().encode(body), signature);
		expect(valid).toBe(true);
	});

	it("rejects an invalid signature", async () => {
		const body = '{"action":"opened"}';
		const valid = await verifyGitHubSignature(secret, new TextEncoder().encode(body), "sha256=bad");
		expect(valid).toBe(false);
	});

	it("rejects an empty signature header", async () => {
		const body = '{"action":"opened"}';
		const valid = await verifyGitHubSignature(secret, new TextEncoder().encode(body), "");
		expect(valid).toBe(false);
	});
});

describe("constantTimeEqual", () => {
	it("returns true for equal strings", () => {
		expect(constantTimeEqual("abc123", "abc123")).toBe(true);
	});

	it("returns false for differing strings of equal length", () => {
		expect(constantTimeEqual("abc123", "abc124")).toBe(false);
	});

	it("returns false for differing lengths", () => {
		expect(constantTimeEqual("abc", "abcd")).toBe(false);
	});

	it("returns true for two empty strings", () => {
		expect(constantTimeEqual("", "")).toBe(true);
	});
});

describe("bubble signature", () => {
	const secret = "bkey_test_secret";
	const algo = "hmac-sha256";
	const nonce = "nonce-1";
	const method = "POST";
	const path = "/events/inbox/manager";
	const body = '{"text":"hi"}';

	function now(): string {
		return String(Math.floor(Date.now() / 1000));
	}

	it("round-trips: a built signature verifies", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
		const ok = await verifyBubbleSignature({
			secret, algo, timestamp, nonce, method, path, body, signature,
		});
		expect(ok).toBe(true);
	});

	it("rejects a tampered body", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
		const ok = await verifyBubbleSignature({
			secret, algo, timestamp, nonce, method, path,
			body: '{"text":"tampered"}', signature,
		});
		expect(ok).toBe(false);
	});

	it("rejects a wrong secret", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
		const ok = await verifyBubbleSignature({
			secret: "bkey_other", algo, timestamp, nonce, method, path, body, signature,
		});
		expect(ok).toBe(false);
	});

	it("rejects a stale timestamp (replay window)", async () => {
		const timestamp = String(Math.floor(Date.now() / 1000) - 600);
		const signature = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
		const ok = await verifyBubbleSignature({
			secret, algo, timestamp, nonce, method, path, body, signature,
		});
		expect(ok).toBe(false);
	});

	it("rejects an unknown algorithm", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
		const ok = await verifyBubbleSignature({
			secret, algo: "none", timestamp, nonce, method, path, body, signature,
		});
		expect(ok).toBe(false);
	});

	it("rejects a missing nonce", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, "", method, path, body);
		const ok = await verifyBubbleSignature({
			secret, algo, timestamp, nonce: "", method, path, body, signature,
		});
		expect(ok).toBe(false);
	});

	it("binds the method (POST sig fails as GET)", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, nonce, "POST", path, body);
		const ok = await verifyBubbleSignature({
			secret, algo, timestamp, nonce, method: "GET", path, body, signature,
		});
		expect(ok).toBe(false);
	});

	it("binds the path (different path fails)", async () => {
		const timestamp = now();
		const signature = await buildBubbleSignature(secret, timestamp, nonce, method, path, body);
		const ok = await verifyBubbleSignature({
			secret, algo, timestamp, nonce, method,
			path: "/events/inbox/other", body, signature,
		});
		expect(ok).toBe(false);
	});

	// PARITY VECTOR — keep identical to tests/test_signing.py (GOLDEN_*).
	// If this drifts from the Python signer, one of the two suites fails here
	// instead of producing silent 403s only visible against a live server.
	it("matches the cross-language golden vector", async () => {
		const sig = await buildBubbleSignature(
			"bkey_golden",
			"1700000000",
			"abc123",
			"POST",
			"/events/inbox/manager",
			'{"payload":{"a":2,"text":"hi","z":1},"source":"inbox"}',
		);
		expect(sig).toBe(
			"81915dcbcceb5cfa052c2a17557962413517be26f0094dd62fe355cd6d0126d7",
		);
	});
});

describe("subscriptionKeysForEvent", () => {
	it("returns topics when present", () => {
		const keys = subscriptionKeysForEvent({
			v: 2, id: "1", source: "github", type: "github.issues",
			topics: ["github:org/repo"], delivery: "bulk", text: "test",
			timestamp: "", payload: {},
		});
		expect(keys).toEqual(["github:org/repo"]);
	});

	it("falls back to type when topics is empty", () => {
		const keys = subscriptionKeysForEvent({
			v: 2, id: "1", source: "unknown", type: "test",
			topics: [], delivery: "bulk", text: "",
			timestamp: "", payload: {},
		});
		expect(keys).toEqual(["test"]);
	});

	it("routes generic topic events like email/received", () => {
		const event = createTopicEvent("email/received", {
			source: "monitor",
			payload: { subject: "Hello" },
		});
		const keys = subscriptionKeysForEvent(event);
		expect(keys).toContain("email/received");
	});

	it("subscription keys match existing shapes for github", () => {
		const event = normalizeGitHubPayload("issues", "d-1", {
			action: "opened",
			repository: { full_name: "org/repo" },
		});
		const keys = subscriptionKeysForEvent(event!);
		expect(keys).toEqual(["github:org/repo"]);
	});

	it("subscription keys match existing shapes for slack", () => {
		const result = normalizeSlackPayload({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention", user: "U123",
				channel: "C456", channel_type: "channel",
				text: "hi", ts: "123",
			},
		});
		const keys = subscriptionKeysForEvent(result.event!);
		expect(keys).toEqual(["slack:T123", "slack:T123:C456"]);
	});

	it("subscription keys match existing shapes for linear", () => {
		const event = normalizeLinearPayload({
			action: "update", type: "Issue",
			data: { team: { key: "PROJ" } },
		});
		const keys = subscriptionKeysForEvent(event);
		expect(keys).toEqual(["linear:PROJ"]);
	});
});

describe("createTopicEvent", () => {
	it("creates v2 event with topic as type", () => {
		const event = createTopicEvent("my-topic", {
			source: "my-app",
			payload: { key: "value" },
		});
		expect(event.v).toBe(2);
		expect(event.type).toBe("my-topic");
		expect(event.source).toBe("my-app");
		expect(event.delivery).toBe("bulk");
		expect(event.payload).toEqual({ key: "value" });
		expect(event.id).toBeTruthy();
		expect(event.timestamp).toBeTruthy();
		expect(event.topics).toEqual(["my-topic", "my-app/my-topic"]);
	});

	it("uses body as payload when no payload field", () => {
		const event = createTopicEvent("test", { foo: "bar" });
		expect(event.payload).toEqual({ foo: "bar" });
	});

	it("routes path-topic events on both the bare and source-qualified topic", () => {
		// The topic contract: publishers strip the source into the body
		// (POST /events/<type> with {"source": ...}), so subscriptions
		// written as the full "source/type" event string must still match.
		const event = createTopicEvent("support.email", {
			source: "monitor",
			payload: { summary: "new email" },
		});
		expect(event.topics).toEqual(["support.email", "monitor/support.email"]);
	});

	it("does not double-qualify when the path already carries the source", () => {
		const event = createTopicEvent("monitor/support.email", {
			source: "monitor",
			payload: {},
		});
		expect(event.topics).toEqual(["monitor/support.email"]);
	});

	it("routes inter-agent inbox events on exactly inbox/<session> (comms-v1 seam)", () => {
		// publish_inbox() POSTs to /events/inbox/<session> with source "inbox".
		// The routing key MUST be byte-identical to the subscription key a
		// session registers (inbox/<session>) — the server matches exactly.
		const event = createTopicEvent("inbox/engineer-42-implement", {
			source: "inbox",
			payload: { id: "m1", sender: "manager", text: "ping", wait: false },
		});
		const keys = subscriptionKeysForEvent(event);
		expect(keys).toEqual(["inbox/engineer-42-implement"]);
		expect(event.payload).toEqual({
			id: "m1", sender: "manager", text: "ping", wait: false,
		});
	});

	it("routes async-ask replies on exactly reply/<uuid> (comms-v1 #269 seam)", () => {
		// publish_reply() POSTs to /events/reply/<uuid> with source "reply".
		// The blocking sender subscribed to (reply/<uuid>); the routing key
		// MUST match it byte-for-byte. Because the topic already starts with
		// "reply/", the source-qualified form is suppressed (no reply/reply/…).
		const event = createTopicEvent("reply/8f3a2b1c9d0e4f5a", {
			source: "reply",
			payload: { corr_id: "0192abc-deadbeef", response: "the answer" },
		});
		const keys = subscriptionKeysForEvent(event);
		expect(keys).toEqual(["reply/8f3a2b1c9d0e4f5a"]);
		expect(event.payload).toEqual({
			corr_id: "0192abc-deadbeef", response: "the answer",
		});
	});

	it("omits the qualified topic when no source is given", () => {
		const event = createTopicEvent("support.email", { payload: {} });
		expect(event.topics).toEqual(["support.email"]);
	});

	it("routing fields suppress path topics entirely", () => {
		const event = createTopicEvent("deploy.complete", {
			source: "ci",
			repo: "org/repo",
			payload: {},
		});
		expect(event.topics).toEqual(["github:org/repo"]);
	});

	it("generates topics from routing fields in body", () => {
		const event = createTopicEvent("deploy.complete", {
			repo: "org/repo",
			workspace: "T123",
			payload: { status: "success" },
		});
		expect(event.topics).toContain("github:org/repo");
		expect(event.topics).toContain("slack:T123");
	});

	it("uses provided id when present", () => {
		const event = createTopicEvent("test", { id: "custom-id" });
		expect(event.id).toBe("custom-id");
	});

	it("defaults source to custom", () => {
		const event = createTopicEvent("test", {});
		expect(event.source).toBe("custom");
	});

	it("passes through text, fields, delivery, run_key", () => {
		const event = createTopicEvent("test", {
			text: "Something happened",
			fields: { key: "val" },
			delivery: "chat",
			run_key: "run-123",
		});
		expect(event.text).toBe("Something happened");
		expect(event.fields).toEqual({ key: "val" });
		expect(event.delivery).toBe("chat");
		expect(event.run_key).toBe("run-123");
	});
});

// ---------------------------------------------------------------------------
// Mock storage adapter for handler tests
// ---------------------------------------------------------------------------

function createMockStorage(): StorageAdapter & {
	deployments: Map<string, DeploymentRecord>;
	apiKeyIndex: Map<string, string>;
	subscriptions: Map<string, Set<string>>;
	bubbles: Map<string, BubbleRecord>;
	slackWorkspaces: Map<string, SlackWorkspaceRecord>;
	delivered: NormalizedEvent[];
	deliveredTo: Array<{ event: NormalizedEvent; ids: string[] }>;
	initCalls: Array<{ deploymentId: string; subscriptions: string[] }>;
} {
	const deployments = new Map<string, DeploymentRecord>();
	const apiKeyIndex = new Map<string, string>();
	const subscriptions = new Map<string, Set<string>>();
	const bubbles = new Map<string, BubbleRecord>();
	const slackWorkspaces = new Map<string, SlackWorkspaceRecord>();
	const delivered: NormalizedEvent[] = [];
	const deliveredTo: Array<{ event: NormalizedEvent; ids: string[] }> = [];
	const initCalls: Array<{ deploymentId: string; subscriptions: string[] }> = [];

	return {
		deployments,
		apiKeyIndex,
		subscriptions,
		bubbles,
		slackWorkspaces,
		delivered,
		deliveredTo,
		initCalls,

		async getDeploymentByApiKey(apiKey: string) {
			const id = apiKeyIndex.get(apiKey);
			if (!id) return null;
			return deployments.get(id) || null;
		},
		async getDeploymentByName(name: string, bubbleId: string) {
			for (const dep of deployments.values()) {
				if (dep.name === name && dep.bubble_id === bubbleId) return { ...dep };
			}
			return null;
		},
		async putDeployment(dep: DeploymentRecord) {
			deployments.set(dep.id, { ...dep });
			apiKeyIndex.set(dep.api_key, dep.id);
		},
		async removeDeployment(dep: DeploymentRecord) {
			deployments.delete(dep.id);
			apiKeyIndex.delete(dep.api_key);
		},
		async addSubscription(key: string, deploymentId: string) {
			if (!subscriptions.has(key)) subscriptions.set(key, new Set());
			subscriptions.get(key)!.add(deploymentId);
		},
		async removeSubscription(key: string, deploymentId: string) {
			const set = subscriptions.get(key);
			if (set) {
				set.delete(deploymentId);
				if (set.size === 0) subscriptions.delete(key);
			}
		},
		async deliver(event: NormalizedEvent) {
			// Realistic fan-out: resolve subscribers via the SAME namespacing the
			// server uses, so bubble isolation is testable at the handler layer.
			delivered.push(event);
			const ids = new Set<string>();
			for (const key of subscriptionKeysForEvent(event)) {
				for (const id of subscriptions.get(key) || []) ids.add(id);
			}
			deliveredTo.push({ event, ids: [...ids] });
			return ids.size;
		},
		async getBubble(bubbleId: string) {
			return bubbles.get(bubbleId) || null;
		},
		async putBubble(bubble: BubbleRecord) {
			bubbles.set(bubble.id, bubble);
		},
		async getSlackWorkspace(workspaceId: string) {
			return slackWorkspaces.get(workspaceId) || null;
		},
		async putSlackWorkspace(workspaceId: string, record: SlackWorkspaceRecord) {
			slackWorkspaces.set(workspaceId, record);
		},
		async initDeploymentSession(deploymentId: string, subs: string[]) {
			initCalls.push({ deploymentId, subscriptions: subs });
		},
	};
}

// --- bubble-signing test helpers -------------------------------------------

// Put a bubble in the store and return it (the "minted" bubble for a test).
function seedBubble(store: ReturnType<typeof createMockStorage>, id = "bub_test", key = "bkey_test"): BubbleRecord {
	const bubble: BubbleRecord = { id, key };
	store.bubbles.set(id, bubble);
	return bubble;
}

// Build a valid signed BubbleAuthContext for a request.
async function signCtx(
	bubble: BubbleRecord,
	method: string,
	path: string,
	rawBody: string,
	nonce = "n1",
): Promise<BubbleAuthContext> {
	const timestamp = String(Math.floor(Date.now() / 1000));
	const signature = await buildBubbleSignature(bubble.key, timestamp, nonce, method, path, rawBody);
	return {
		bubbleId: bubble.id,
		algo: "hmac-sha256",
		timestamp,
		nonce,
		signature,
		method,
		path,
		rawBody,
	};
}

// An unsigned context — registration treats this as a MINT.
function mintCtx(method: string, path: string, rawBody: string): BubbleAuthContext {
	return { bubbleId: "", algo: "", timestamp: "", nonce: "", signature: "", method, path, rawBody };
}

// ---------------------------------------------------------------------------
// Handler tests
// ---------------------------------------------------------------------------

describe("authenticateDeployment", () => {
	it("returns deployment when api key and id match", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test", subscriptions: [] };
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await authenticateDeployment(store, "key1", "d1");
		expect(result).not.toBeNull();
		expect(result!.id).toBe("d1");
	});

	it("returns null for unknown api key", async () => {
		const store = createMockStorage();
		expect(await authenticateDeployment(store, "bad", "d1")).toBeNull();
	});

	it("returns null when deployment id does not match key", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test", subscriptions: [] };
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		expect(await authenticateDeployment(store, "key1", "wrong-id")).toBeNull();
	});
});

describe("handleGitHubWebhook", () => {
	it("normalizes and delivers a v2 github event", async () => {
		const store = createMockStorage();
		// Webhook resource topics are GLOBAL — a subscriber on github:org/repo
		// receives it regardless of bubble.
		await store.addSubscription("github:org/repo", "sub1");
		const result = await handleGitHubWebhook(store, "issues", "del-1", {
			action: "opened",
			repository: { full_name: "org/repo" },
		});
		expect(result.status).toBe(200);
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("github.issues");
		expect(store.delivered[0].v).toBe(2);
		expect(store.delivered[0].topics).toEqual(["github:org/repo"]);
	});

	it("returns 400 for payload without repository", async () => {
		const store = createMockStorage();
		const result = await handleGitHubWebhook(store, "push", "del-1", { action: "opened" });
		expect(result.status).toBe(400);
	});
});

describe("handleLinearWebhook", () => {
	it("normalizes and delivers a v2 linear event", async () => {
		const store = createMockStorage();
		const result = await handleLinearWebhook(store, {
			action: "update",
			type: "Issue",
			data: { team: { key: "PROJ" } },
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("linear.Issue.update");
		expect(store.delivered[0].v).toBe(2);
		expect(store.delivered[0].topics).toEqual(["linear:PROJ"]);
	});
});

describe("handleSlackWebhook", () => {
	it("delivers a v2 slack mention event with chat delivery", async () => {
		const store = createMockStorage();
		const result = await handleSlackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "hello",
				ts: "123",
			},
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].delivery).toBe("chat");
		// Webhook resource topic is global; a subscriber on it would receive it.
		await store.addSubscription(store.delivered[0].topics[0], "sub1");
		expect(await store.deliver(store.delivered[0])).toBe(1);
	});

	it("returns ok for non-event_callback types without delivering", async () => {
		const store = createMockStorage();
		const result = await handleSlackWebhook(store, { type: "app_rate_limited" });
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(0);
	});

	it("filters self-bot messages using stored workspace bot_id", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T123", { bot_token: "xoxb-test", bot_id: "BSELF" });

		const result = await handleSlackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention",
				user: "U123",
				bot_id: "BSELF",
				channel: "C456",
				channel_type: "channel",
				text: "echo",
				ts: "123",
			},
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(0);
	});

	it("returns challenge for url_verification payload", async () => {
		const store = createMockStorage();
		const result = await handleSlackWebhook(store, {
			type: "url_verification",
			challenge: "test-challenge",
		});
		expect(result.status).toBe(200);
		expect((result.body as Record<string, string>).challenge).toBe("test-challenge");
	});

	// Multi-bot workspace (incident 2026-06-24): two bots registered in one
	// workspace must EACH have their own messages filtered. The record is keyed
	// by api_app_id, so the second bot to register no longer clobbers the first.
	it("filters either bot's messages when the workspace has multiple registered bots", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T123", {
			bots: {
				A1: { bot_token: "xoxb-1", bot_id: "B1", app_id: "A1" },
				A2: { bot_token: "xoxb-2", bot_id: "B2", app_id: "A2" },
			},
		});

		const fromBot = async (appId: string, bid: string) =>
			handleSlackWebhook(store, {
				type: "event_callback",
				team_id: "T123",
				api_app_id: appId,
				event: {
					type: "message",
					user: "U1",
					bot_id: bid,
					channel: "C456",
					channel_type: "channel",
					thread_ts: "100.000",
					text: "loop",
					ts: "123",
				},
			});

		await fromBot("A1", "B1"); // bot 1's own message, via bot 1's app webhook
		await fromBot("A2", "B2"); // bot 2's own message, via bot 2's app webhook
		expect(store.delivered).toHaveLength(0); // both self bots filtered

		await fromBot("A1", "B_THIRDPARTY"); // a third-party bot seen by app 1
		expect(store.delivered).toHaveLength(1); // passes through
	});
});

describe("handleRegisterDeployment", () => {
	it("MINTS a bubble for an unsigned registration and returns the key once", async () => {
		const store = createMockStorage();
		const body = { name: "my-deploy", subscriptions: ["github:org/repo", "linear:PROJ"] };
		const raw = JSON.stringify(body);
		const result = await handleRegisterDeployment(store, body, mintCtx("POST", "/deployments", raw));
		expect(result.status).toBe(201);
		const resp = result.body as { deployment_id: string; api_key: string; bubble_id: string; bubble_key: string };
		expect(resp.deployment_id).toBeTruthy();
		expect(resp.api_key).toMatch(/^moda_/);
		expect(resp.bubble_id).toMatch(/^bub_/);
		expect(resp.bubble_key).toMatch(/^bkey_/); // returned ONCE at mint
		expect(store.bubbles.has(resp.bubble_id)).toBe(true);

		expect(store.deployments.size).toBe(1);
		// Global webhook topics are not namespaced.
		expect(store.subscriptions.get("github:org/repo")?.has(resp.deployment_id)).toBe(true);
		expect(store.subscriptions.get("linear:PROJ")?.has(resp.deployment_id)).toBe(true);
		expect(store.initCalls).toHaveLength(1);
		expect(store.initCalls[0].deploymentId).toBe(resp.deployment_id);
	});

	it("namespaces non-global subscriptions to the bubble", async () => {
		const store = createMockStorage();
		const body = { name: "d", subscriptions: ["inbox/manager", "monitor/x"] };
		const raw = JSON.stringify(body);
		const result = await handleRegisterDeployment(store, body, mintCtx("POST", "/deployments", raw));
		const resp = result.body as { deployment_id: string; bubble_id: string };
		expect(store.subscriptions.get(`${resp.bubble_id}:inbox/manager`)?.has(resp.deployment_id)).toBe(true);
		expect(store.subscriptions.get("inbox/manager")).toBeUndefined(); // never bare
	});

	it("JOINS an existing bubble with a valid signature (no key returned)", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store);
		const body = { name: "second-session", subscriptions: ["inbox/worker"] };
		const raw = JSON.stringify(body);
		const ctx = await signCtx(bubble, "POST", "/deployments", raw);
		const result = await handleRegisterDeployment(store, body, ctx);
		expect(result.status).toBe(201);
		const resp = result.body as { bubble_id: string; bubble_key?: string };
		expect(resp.bubble_id).toBe(bubble.id);
		expect(resp.bubble_key).toBeUndefined(); // never on join
		expect(store.bubbles.size).toBe(1); // no new bubble minted
	});

	it("REJECTS a registration with partial signing headers (no silent mint)", async () => {
		const store = createMockStorage();
		const body = { name: "x", subscriptions: ["inbox/x"] };
		const raw = JSON.stringify(body);
		// bubble_id present but signature/timestamp/nonce missing — malformed,
		// must NOT fall back to minting a fresh bubble.
		const ctx = { ...mintCtx("POST", "/deployments", raw), bubbleId: "bub_partial" };
		const result = await handleRegisterDeployment(store, body, ctx);
		expect(result.status).toBe(403);
		expect(store.bubbles.size).toBe(0);
	});

	it("REJECTS a join with a bad signature", async () => {
		const store = createMockStorage();
		seedBubble(store, "bub_test", "bkey_real");
		const body = { name: "x", subscriptions: ["inbox/worker"] };
		const raw = JSON.stringify(body);
		// Sign with the wrong key but claim the real bubble id.
		const ctx = await signCtx({ id: "bub_test", key: "bkey_wrong" }, "POST", "/deployments", raw);
		const result = await handleRegisterDeployment(store, body, ctx);
		expect(result.status).toBe(403);
	});

	it("rejects missing name", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, { subscriptions: ["foo"] }, mintCtx("POST", "/deployments", ""));
		expect(result.status).toBe(400);
	});

	it("rejects missing subscriptions", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, { name: "test" }, mintCtx("POST", "/deployments", ""));
		expect(result.status).toBe(400);
	});

	it("rejects empty subscriptions array", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, { name: "test", subscriptions: [] }, mintCtx("POST", "/deployments", ""));
		expect(result.status).toBe(400);
	});

	it("supersedes a prior deployment with the same name in the same bubble (#278)", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store);

		// First registration — joins the existing bubble.
		const body1 = { name: "worker", subscriptions: ["inbox/worker"] };
		const raw1 = JSON.stringify(body1);
		const ctx1 = await signCtx(bubble, "POST", "/deployments", raw1);
		const r1 = await handleRegisterDeployment(store, body1, ctx1);
		expect(r1.status).toBe(201);
		const dep1 = (r1.body as Record<string, string>).deployment_id;

		// Sanity: one deployment, one subscription entry.
		expect(store.deployments.size).toBe(1);
		expect(store.subscriptions.get(`${bubble.id}:inbox/worker`)?.has(dep1)).toBe(true);

		// Re-register with the same name (simulates --fresh restart).
		const body2 = { name: "worker", subscriptions: ["inbox/worker", "github:org/repo"] };
		const raw2 = JSON.stringify(body2);
		const ctx2 = await signCtx(bubble, "POST", "/deployments", raw2, "n2");
		const r2 = await handleRegisterDeployment(store, body2, ctx2);
		expect(r2.status).toBe(201);
		const dep2 = (r2.body as Record<string, string>).deployment_id;

		// The old deployment must be gone — exactly one deployment remains.
		expect(store.deployments.size).toBe(1);
		expect(store.deployments.has(dep1)).toBe(false);
		expect(store.deployments.has(dep2)).toBe(true);

		// Old subscription entries are cleaned up.
		expect(store.subscriptions.get(`${bubble.id}:inbox/worker`)?.has(dep1)).toBeFalsy();
		// New subscription entries are present.
		expect(store.subscriptions.get(`${bubble.id}:inbox/worker`)?.has(dep2)).toBe(true);
		expect(store.subscriptions.get("github:org/repo")?.has(dep2)).toBe(true);
	});

	it("does not supersede a deployment with the same name in a different bubble", async () => {
		const store = createMockStorage();
		const bubbleA = seedBubble(store, "bub_a", "bkey_a");
		const bubbleB = seedBubble(store, "bub_b", "bkey_b");

		const body = { name: "worker", subscriptions: ["inbox/worker"] };

		const rawA = JSON.stringify(body);
		const ctxA = await signCtx(bubbleA, "POST", "/deployments", rawA);
		const rA = await handleRegisterDeployment(store, body, ctxA);
		expect(rA.status).toBe(201);

		const rawB = JSON.stringify(body);
		const ctxB = await signCtx(bubbleB, "POST", "/deployments", rawB);
		const rB = await handleRegisterDeployment(store, body, ctxB);
		expect(rB.status).toBe(201);

		// Both deployments coexist — different bubbles.
		expect(store.deployments.size).toBe(2);
	});
});

describe("handleUpdateSubscriptions", () => {
	it("adds new subscriptions to an existing deployment", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test",
			subscriptions: ["github:org/repo"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await handleUpdateSubscriptions(store, "d1", "key1", {
			add: ["linear:PROJ"],
		});
		expect(result.status).toBe(200);
		const body = result.body as { subscriptions: string[]; added: number };
		expect(body.added).toBe(1);
		expect(body.subscriptions).toContain("github:org/repo");
		expect(body.subscriptions).toContain("linear:PROJ");
	});

	it("namespaces a non-global added subscription to the deployment's bubble", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", bubble_id: "bub_abc",
			subscriptions: [],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		await handleUpdateSubscriptions(store, "d1", "key1", { add: ["inbox/d1"] });
		expect(store.subscriptions.get("bub_abc:inbox/d1")?.has("d1")).toBe(true);
		expect(store.subscriptions.get("inbox/d1")).toBeUndefined();
	});

	it("deduplicates existing subscriptions", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test",
			subscriptions: ["github:org/repo"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await handleUpdateSubscriptions(store, "d1", "key1", {
			add: ["github:org/repo", "linear:PROJ"],
		});
		const body = result.body as { subscriptions: string[]; added: number };
		expect(body.added).toBe(1);
		expect(body.subscriptions).toHaveLength(2);
	});

	it("rejects unauthorized requests", async () => {
		const store = createMockStorage();
		const result = await handleUpdateSubscriptions(store, "d1", "bad-key", { add: ["foo"] });
		expect(result.status).toBe(403);
	});

	it("rejects empty add array", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test", subscriptions: [] };
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await handleUpdateSubscriptions(store, "d1", "key1", { add: [] });
		expect(result.status).toBe(400);
	});

	it("persists updated deployment via putDeployment", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test",
			subscriptions: ["github:org/repo"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		await handleUpdateSubscriptions(store, "d1", "key1", { add: ["slack:T1"] });
		const saved = store.deployments.get("d1")!;
		expect(saved.subscriptions).toContain("slack:T1");
	});
});

describe("handleDeregisterDeployment", () => {
	it("removes deployment and its subscription-index entries", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1",
			subscriptions: ["github:org/repo", "linear:PROJ"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");
		store.subscriptions.set("github:org/repo", new Set(["d1"]));
		store.subscriptions.set("linear:PROJ", new Set(["d1"]));

		const result = await handleDeregisterDeployment(store, "d1", "key1");
		expect(result.status).toBe(200);
		expect((result.body as Record<string, boolean>).ok).toBe(true);

		expect(store.deployments.size).toBe(0);
		expect(store.apiKeyIndex.size).toBe(0);
		expect(store.subscriptions.has("github:org/repo")).toBe(false);
		expect(store.subscriptions.has("linear:PROJ")).toBe(false);
	});

	it("rejects unauthorized requests (wrong api key)", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", subscriptions: [],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await handleDeregisterDeployment(store, "d1", "wrong-key");
		expect(result.status).toBe(403);
		expect(store.deployments.size).toBe(1);
	});

	it("rejects unknown deployment id", async () => {
		const store = createMockStorage();
		const result = await handleDeregisterDeployment(store, "nonexistent", "any-key");
		expect(result.status).toBe(403);
	});

	it("does not affect other deployments sharing a subscription key", async () => {
		const store = createMockStorage();
		const dep1: DeploymentRecord = {
			id: "d1", name: "a", api_key: "key1",
			subscriptions: ["github:org/repo"],
		};
		const dep2: DeploymentRecord = {
			id: "d2", name: "b", api_key: "key2",
			subscriptions: ["github:org/repo"],
		};
		store.deployments.set("d1", dep1);
		store.deployments.set("d2", dep2);
		store.apiKeyIndex.set("key1", "d1");
		store.apiKeyIndex.set("key2", "d2");
		store.subscriptions.set("github:org/repo", new Set(["d1", "d2"]));

		await handleDeregisterDeployment(store, "d1", "key1");
		expect(store.deployments.size).toBe(1);
		expect(store.deployments.has("d2")).toBe(true);
		expect(store.subscriptions.get("github:org/repo")?.has("d2")).toBe(true);
		expect(store.subscriptions.get("github:org/repo")?.has("d1")).toBe(false);
	});
});

describe("handleTopicEvent", () => {
	it("requires a valid bubble signature to publish", async () => {
		const store = createMockStorage();
		const body = { source: "ci", payload: { sha: "abc" } };
		const raw = JSON.stringify(body);
		const result = await handleTopicEvent(store, "deploy.complete", body, mintCtx("POST", "/events/deploy.complete", raw));
		expect(result.status).toBe(403); // unsigned publish rejected
		expect(store.delivered).toHaveLength(0);
	});

	it("creates and delivers a v2 topic event stamped with the publisher's bubble", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store);
		const body = { source: "ci", payload: { sha: "abc" } };
		const raw = JSON.stringify(body);
		const ctx = await signCtx(bubble, "POST", "/events/deploy.complete", raw);
		const result = await handleTopicEvent(store, "deploy.complete", body, ctx);
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("deploy.complete");
		expect(store.delivered[0].bubble_id).toBe(bubble.id);
	});

	it("isolates: a publish reaches only same-bubble subscribers of the topic", async () => {
		const store = createMockStorage();
		const bubbleA = seedBubble(store, "bub_A", "bkey_A");
		seedBubble(store, "bub_B", "bkey_B");
		// Subscriber in bubble A and subscriber in bubble B both want inbox/x.
		await store.addSubscription(namespaceSubKey("bub_A", "inbox/x"), "depA");
		await store.addSubscription(namespaceSubKey("bub_B", "inbox/x"), "depB");

		const body = { payload: { text: "hi" } };
		const raw = JSON.stringify(body);
		const ctx = await signCtx(bubbleA, "POST", "/events/inbox/x", raw);
		await handleTopicEvent(store, "inbox/x", body, ctx);

		const last = store.deliveredTo[store.deliveredTo.length - 1];
		expect(last.ids).toEqual(["depA"]); // bubble B's subscriber excluded
	});
});

describe("handleSlackSend", () => {
	it("rejects missing channel", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { text: "hi", workspace: "T1" });
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("channel");
	});

	it("rejects missing text", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { channel: "C1", workspace: "T1" });
		expect(result.status).toBe(400);
	});

	it("rejects missing workspace", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { channel: "C1", text: "hi" });
		expect(result.status).toBe(400);
	});

	it("rejects unknown workspace", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { channel: "C1", text: "hi", workspace: "T404" });
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("bot token");
	});
});

describe("handleSlackWorkspaceRegister", () => {
	it("rejects missing workspace_id", async () => {
		const store = createMockStorage();
		const result = await handleSlackWorkspaceRegister(store, { bot_token: "xoxb-test" });
		expect(result.status).toBe(400);
	});

	it("rejects missing bot_token", async () => {
		const store = createMockStorage();
		const result = await handleSlackWorkspaceRegister(store, { workspace_id: "T1" });
		expect(result.status).toBe(400);
	});

	it("uses explicit bot_id without calling auth.test", async () => {
		const store = createMockStorage();
		const result = await handleSlackWorkspaceRegister(store, {
			workspace_id: "T_EXPLICIT",
			bot_token: "xoxb-test",
			bot_id: "B_EXPLICIT",
		});
		expect(result.status).toBe(200);
		const body = result.body as Record<string, unknown>;
		expect(body.bot_id).toBe("B_EXPLICIT");
		const ws = store.slackWorkspaces.get("T_EXPLICIT");
		expect(ws?.bot_id).toBe("B_EXPLICIT");
	});

	// Incident 2026-06-24: a second bot registering the same workspace must NOT
	// clobber the first. Both bots accrete into the api_app_id-keyed map, and
	// BOTH stay self-filtered (so neither loops on its own messages).
	it("registering a second bot does not clobber the first", async () => {
		const store = createMockStorage();
		await handleSlackWorkspaceRegister(store, {
			workspace_id: "T1", bot_token: "xoxb-1", bot_id: "B1", app_id: "A1",
			signing_secret: "sec-1",
		});
		await handleSlackWorkspaceRegister(store, {
			workspace_id: "T1", bot_token: "xoxb-2", bot_id: "B2", app_id: "A2",
			signing_secret: "sec-2",
		});

		const ws = store.slackWorkspaces.get("T1");
		expect(ws?.bots?.A1?.bot_id).toBe("B1");
		expect(ws?.bots?.A1?.bot_token).toBe("xoxb-1");
		expect(ws?.bots?.A2?.bot_id).toBe("B2");
		expect(ws?.bots?.A2?.signing_secret).toBe("sec-2");

		// The first bot's own messages are still filtered after the second registers.
		const result = await handleSlackWebhook(store, {
			type: "event_callback",
			team_id: "T1",
			api_app_id: "A1",
			event: {
				type: "message",
				user: "U1",
				bot_id: "B1",
				channel: "C1",
				channel_type: "channel",
				thread_ts: "100.000",
				text: "should be filtered",
				ts: "123",
			},
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(0);
	});
});

describe("resolveSlackSigningSecret", () => {
	it("uses the authoring app's per-app secret", () => {
		const ws = {
			bots: {
				A1: { bot_token: "x1", bot_id: "B1", signing_secret: "sec-1", app_id: "A1" },
				A2: { bot_token: "x2", bot_id: "B2", signing_secret: "sec-2", app_id: "A2" },
			},
		};
		expect(resolveSlackSigningSecret(ws, { api_app_id: "A2" }, "global")).toBe("sec-2");
	});

	it("falls back to the global secret for an unregistered app", () => {
		const ws = { bots: { A1: { bot_token: "x1", bot_id: "B1", app_id: "A1" } } };
		expect(resolveSlackSigningSecret(ws, { api_app_id: "A_UNKNOWN" }, "global")).toBe("global");
	});

	it("falls back to the global secret for a legacy single-bot record", () => {
		const ws = { bot_token: "x", bot_id: "BSELF" };
		expect(resolveSlackSigningSecret(ws, { api_app_id: "A1" }, "global")).toBe("global");
	});
});

describe("auth rejection counters", () => {
	beforeEach(() => {
		resetAuthRejectionCounters();
	});

	it("starts at zero", () => {
		const c = getAuthRejectionCounters();
		expect(c.bad_signature).toBe(0);
		expect(c.stale_timestamp).toBe(0);
		expect(c.unknown_bubble).toBe(0);
	});

	it("increments bad_signature on wrong key", async () => {
		const store = createMockStorage();
		seedBubble(store, "bub_test", "bkey_real");
		const body = '{"text":"hi"}';
		const ctx = await signCtx({ id: "bub_test", key: "bkey_wrong" }, "POST", "/events/x", body);
		await authenticateBubble(store, ctx);
		const c = getAuthRejectionCounters();
		expect(c.bad_signature).toBe(1);
		expect(c.stale_timestamp).toBe(0);
		expect(c.unknown_bubble).toBe(0);
	});

	it("increments stale_timestamp on expired signature", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store);
		const staleTs = String(Math.floor(Date.now() / 1000) - 600);
		const signature = await buildBubbleSignature(bubble.key, staleTs, "n1", "POST", "/events/x", "{}");
		const ctx: BubbleAuthContext = {
			bubbleId: bubble.id, algo: "hmac-sha256", timestamp: staleTs,
			nonce: "n1", signature, method: "POST", path: "/events/x", rawBody: "{}",
		};
		await authenticateBubble(store, ctx);
		const c = getAuthRejectionCounters();
		expect(c.stale_timestamp).toBe(1);
		expect(c.bad_signature).toBe(0);
	});

	it("increments unknown_bubble when bubble_id not found", async () => {
		const store = createMockStorage();
		const ctx = await signCtx({ id: "bub_nonexistent", key: "bkey_x" }, "POST", "/events/x", "{}");
		await authenticateBubble(store, ctx);
		const c = getAuthRejectionCounters();
		expect(c.unknown_bubble).toBe(1);
		expect(c.bad_signature).toBe(0);
	});

	it("does not increment on successful authentication", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store);
		const ctx = await signCtx(bubble, "POST", "/events/x", "{}");
		const result = await authenticateBubble(store, ctx);
		expect(result).not.toBeNull();
		const c = getAuthRejectionCounters();
		expect(c.bad_signature).toBe(0);
		expect(c.stale_timestamp).toBe(0);
		expect(c.unknown_bubble).toBe(0);
	});

	it("returns a copy, not the internal state", () => {
		const c = getAuthRejectionCounters();
		c.bad_signature = 999;
		expect(getAuthRejectionCounters().bad_signature).toBe(0);
	});
});
