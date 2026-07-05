import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
	type StorageAdapter,
	type DeploymentRecord,
	type BubbleRecord,
	type BubbleAuthContext,
	type SlackWorkspaceRecord,
	type ResourceGrant,
	type NormalizedEvent,
	namespaceSubKey,
	parseGlobalTopic,
	normalizeResource,
	handleAuthorizeResource,
	unauthorizedGlobalTopics,
	admittedDeploymentIds,
	createTopicEvent,
	normalizeGitHubPayload,
	normalizeLinearPayload,
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
	handleWebhookRequest,
	matchWebhookSource,
	verifyLinearSignature,
	type InboundWebhookRequest,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleDeregisterDeployment,
	handleTopicEvent,
	handleChannelsSend,
	handleChannelsTyping,
	handleChannelsHistory,
	handleSlackSend,
	handleSlackWorkspaceRegister,
	resolveSlackSigningSecret,
	handleWebhookHandshake,
	handleWhatsAppWebhook,
	handleWhatsAppNumberRegister,
	channelWindowKey,
	whatsappNumberKey,
} from "../src/core";
import { hmacHex } from "./helpers";
import { bridgeSlackWebhook } from "../src/adapters/chat-sdk-slack";
import { setWhatsAppApiUrl } from "../src/channels";

afterEach(() => vi.unstubAllGlobals());

// handleSlackWebhook takes the raw webhook body plus its parsed form (the
// pipeline parses once before verification); tests build payload objects, so
// stringify at the call boundary.
function slackWebhook(store: StorageAdapter, payload: Record<string, unknown>) {
	return handleSlackWebhook(store, JSON.stringify(payload), payload);
}

// The Chat SDK api module form-encodes Slack Web API calls; decode a stubbed
// fetch's request body (form or JSON) back into an object for assertions.
function decodeSlackBody(init?: RequestInit): Record<string, unknown> {
	const raw = String(init?.body ?? "");
	if (raw.startsWith("{")) {
		return JSON.parse(raw || "{}");
	}
	return Object.fromEntries(new URLSearchParams(raw));
}

function stubSlackAuth(teamId: string, botId = "B1", appId = "A1") {
	vi.stubGlobal("fetch", vi.fn(async (url: string | URL) => {
		const u = String(url);
		if (u.includes("/auth.test")) {
			return fetchOk(200, { ok: true, team_id: teamId, bot_id: botId });
		}
		if (u.includes("/bots.info")) {
			return fetchOk(200, { ok: true, bot: { app_id: appId } });
		}
		return fetchOk(200, { ok: true, ts: "1.2" });
	}));
}

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
			sender: { login: "bobi" },
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
		expect(event!.fields!.comment_body).toBe("Rendered spec");
		expect(event!.fields!.sender).toBe("bobi");
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

describe("verifyGitHubSignature", () => {
	const secret = "test-webhook-secret";

	async function sign(body: string): Promise<string> {
		return `sha256=${await hmacHex(secret, body)}`;
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
		const result = bridgeSlackWebhook(JSON.stringify({
			type: "event_callback",
			team_id: "T123",
			event: {
				type: "app_mention", user: "U123",
				channel: "C456", channel_type: "channel",
				text: "hi", ts: "123",
			},
		}));
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

	// #618: a published/forwarded chat event keeps its reply address; a
	// non-string conversation is dropped rather than passed through.
	it("passes a string conversation through and drops non-strings", () => {
		const event = createTopicEvent("relay/chat", {
			source: "relay",
			delivery: "chat",
			text: "forwarded",
			conversation: "slack:T1:channel:C9:thread:1.2",
		});
		expect(event.conversation).toBe("slack:T1:channel:C9:thread:1.2");

		const bad = createTopicEvent("relay/chat", {
			source: "relay",
			conversation: ["slack", "T1"],
		});
		expect(bad.conversation).toBeUndefined();
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
	channelState: Map<string, Record<string, unknown>>;
	resourceGrants: Map<string, ResourceGrant>;
	delivered: NormalizedEvent[];
	deliveredTo: Array<{ event: NormalizedEvent; ids: string[] }>;
	initCalls: Array<{ deploymentId: string; subscriptions: string[] }>;
	/** Test helper: directly seed a grant (bypasses upstream verification). */
	seedGrant(service: string, resource: string, bubbleId: string): void;
} {
	const deployments = new Map<string, DeploymentRecord>();
	const apiKeyIndex = new Map<string, string>();
	const subscriptions = new Map<string, Set<string>>();
	const bubbles = new Map<string, BubbleRecord>();
	const slackWorkspaces = new Map<string, SlackWorkspaceRecord>();
	const channelState = new Map<string, Record<string, unknown>>();
	const resourceGrants = new Map<string, ResourceGrant>();
	const delivered: NormalizedEvent[] = [];
	const deliveredTo: Array<{ event: NormalizedEvent; ids: string[] }> = [];
	const initCalls: Array<{ deploymentId: string; subscriptions: string[] }> = [];

	return {
		deployments,
		apiKeyIndex,
		subscriptions,
		bubbles,
		slackWorkspaces,
		channelState,
		resourceGrants,
		delivered,
		deliveredTo,
		initCalls,

		seedGrant(service: string, resource: string, bubbleId: string) {
			resourceGrants.set(`${service}:${resource}:${bubbleId}`, {
				id: `grant_${service}_${resource}_${bubbleId}`,
				account_id: null,
				bubble_id: bubbleId,
				service: service as ResourceGrant["service"],
				resource,
				granted_by: "upstream_token_verification",
				created_at: "2026-01-01T00:00:00Z",
				expires_at: null,
			});
		},

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
		async getDeploymentById(id: string) {
			const dep = deployments.get(id);
			return dep ? { ...dep } : null;
		},
		async putResourceGrant(grant: ResourceGrant) {
			resourceGrants.set(`${grant.service}:${grant.resource}:${grant.bubble_id}`, grant);
		},
		async hasResourceGrant(service: string, resource: string, bubbleId: string) {
			return resourceGrants.has(`${service}:${resource}:${bubbleId}`);
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
		async getChannelState(key: string) {
			return channelState.get(key) || null;
		},
		async putChannelState(key: string, value: Record<string, unknown>) {
			channelState.set(key, value);
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

// ---------------------------------------------------------------------------
// #639 — the unified inbound webhook pipeline. Both transports (Worker and
// local server) route every /webhooks/<source> request through this one
// function, so these tests ARE the shared verification coverage for both.
// ---------------------------------------------------------------------------
describe("handleWebhookRequest (unified pipeline)", () => {
	function req(rawBody: string, headers: Record<string, string> = {}): InboundWebhookRequest {
		const lower = Object.fromEntries(
			Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]),
		);
		return { rawBody, header: (n) => lower[n.toLowerCase()] || "" };
	}

	it("returns null for an unregistered source (transport falls through to 404)", async () => {
		const store = createMockStorage();
		const result = await handleWebhookRequest(store, "nope", req("{}"), {});
		expect(result).toBeNull();
	});

	it("rejects invalid JSON with 400", async () => {
		const store = createMockStorage();
		const result = await handleWebhookRequest(store, "github", req("not json"), {});
		expect(result?.status).toBe(400);
	});

	it("rejects non-object JSON with 400", async () => {
		const store = createMockStorage();
		for (const raw of ["null", "[1,2]", '"str"']) {
			const result = await handleWebhookRequest(store, "linear", req(raw), {});
			expect(result?.status).toBe(400);
		}
	});

	it("github: verifies and delivers through the pipeline", async () => {
		const store = createMockStorage();
		await store.addSubscription("github:org/repo", "sub1");
		const body = JSON.stringify({ action: "opened", repository: { full_name: "org/repo" } });
		const secrets = { github: "gh-secret" };

		const bad = await handleWebhookRequest(
			store, "github", req(body, { "x-hub-signature-256": "sha256=bad" }), secrets);
		expect(bad?.status).toBe(401);

		const sig = "sha256=" + (await hmacHex("gh-secret", body));
		const ok = await handleWebhookRequest(
			store, "github",
			req(body, { "x-hub-signature-256": sig, "x-github-event": "issues", "x-github-delivery": "d1" }),
			secrets);
		expect(ok?.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("github.issues");
	});

	it("linear: rejects a bad or missing signature and accepts a valid one", async () => {
		const store = createMockStorage();
		await store.addSubscription("linear:ENG", "sub1");
		const body = JSON.stringify({
			action: "update", type: "Issue",
			data: { title: "t", team: { key: "ENG" } },
			webhookTimestamp: Date.now(),
		});
		const secrets = { linear: "ln-secret" };

		const missing = await handleWebhookRequest(store, "linear", req(body), secrets);
		expect(missing?.status).toBe(401);

		const bad = await handleWebhookRequest(
			store, "linear", req(body, { "linear-signature": "deadbeef" }), secrets);
		expect(bad?.status).toBe(401);

		const ok = await handleWebhookRequest(
			store, "linear",
			req(body, { "linear-signature": await hmacHex("ln-secret", body) }), secrets);
		expect(ok?.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].topics).toEqual(["linear:ENG"]);
	});

	it("linear: rejects a replayed request via the signed webhookTimestamp", async () => {
		const store = createMockStorage();
		const body = JSON.stringify({
			action: "update", type: "Issue",
			data: { title: "t", team: { key: "ENG" } },
			webhookTimestamp: Date.now() - 3600_000,
		});
		const result = await handleWebhookRequest(
			store, "linear",
			req(body, { "linear-signature": await hmacHex("ln-secret", body) }),
			{ linear: "ln-secret" });
		expect(result?.status).toBe(401);
	});

	it("linear: fails closed on a signed payload with no numeric webhookTimestamp", async () => {
		// The replay guard must not be skippable: a validly-signed body that
		// omits webhookTimestamp (or carries it as a string) would otherwise be
		// replayable forever.
		const store = createMockStorage();
		for (const extra of [{}, { webhookTimestamp: String(Date.now()) }]) {
			const body = JSON.stringify({
				action: "update", type: "Issue",
				data: { title: "t", team: { key: "ENG" } },
				...extra,
			});
			const result = await handleWebhookRequest(
				store, "linear",
				req(body, { "linear-signature": await hmacHex("ln-secret", body) }),
				{ linear: "ln-secret" });
			expect(result?.status).toBe(401);
		}
	});

	it("linear: admits unverified when no secret is configured (legacy contract)", async () => {
		const store = createMockStorage();
		const body = JSON.stringify({ action: "update", type: "Issue", data: { team: { key: "ENG" } } });
		const result = await handleWebhookRequest(store, "linear", req(body), {});
		expect(result?.status).toBe(200);
	});

	it("slack: url_verification short-circuits before the signature check", async () => {
		const store = createMockStorage();
		const body = JSON.stringify({ type: "url_verification", challenge: "c1" });
		// Secret configured + no signing headers: only preVerify lets this pass.
		const result = await handleWebhookRequest(store, "slack", req(body), { slack: "sl-secret" });
		expect(result?.status).toBe(200);
		expect(result?.body).toEqual({ challenge: "c1" });
	});

	it("slack: retried event deliveries dedup before the signature check", async () => {
		const store = createMockStorage();
		const body = JSON.stringify({ type: "event_callback", event: { type: "message" } });
		const result = await handleWebhookRequest(
			store, "slack", req(body, { "x-slack-retry-num": "1" }), { slack: "sl-secret" });
		expect(result?.status).toBe(200);
		expect(result?.body).toEqual({ ok: true });
	});

	it("slack: rejects an unsigned event when a signing secret is configured", async () => {
		const store = createMockStorage();
		const body = JSON.stringify({ type: "event_callback", event: { type: "message", ts: "1.1" } });
		const result = await handleWebhookRequest(store, "slack", req(body), { slack: "sl-secret" });
		expect(result?.status).toBe(401);
	});
});

describe("matchWebhookSource", () => {
	it("matches registered sources, with or without a trailing slash", () => {
		expect(matchWebhookSource("/webhooks/github")).toBe("github");
		expect(matchWebhookSource("/webhooks/linear")).toBe("linear");
		expect(matchWebhookSource("/webhooks/slack/")).toBe("slack");
	});

	it("returns null for unregistered sources and non-webhook paths", () => {
		expect(matchWebhookSource("/webhooks/telegram")).toBeNull();
		expect(matchWebhookSource("/webhooks/github/extra")).toBeNull();
		expect(matchWebhookSource("/webhooks/")).toBeNull();
		expect(matchWebhookSource("/events/foo")).toBeNull();
	});
});

describe("verifyLinearSignature", () => {
	it("accepts the exact-body HMAC with a fresh timestamp, rejects everything else", async () => {
		const body = '{"a":1}';
		const now = Date.now();
		const sig = await hmacHex("s", body);

		expect(await verifyLinearSignature("s", body, sig, now)).toBe(true);
		expect(await verifyLinearSignature("s", body, "", now)).toBe(false);
		expect(await verifyLinearSignature("s", body, "deadbeef", now)).toBe(false);
		expect(await verifyLinearSignature("s", body + " ", sig, now)).toBe(false);
		expect(await verifyLinearSignature("other", body, sig, now)).toBe(false);
	});

	it("owns the replay window and fails closed on a missing timestamp", async () => {
		const body = '{"a":1}';
		const sig = await hmacHex("s", body);

		expect(await verifyLinearSignature("s", body, sig, Date.now() - 3600_000)).toBe(false);
		expect(await verifyLinearSignature("s", body, sig, undefined)).toBe(false);
		expect(await verifyLinearSignature("s", body, sig, String(Date.now()))).toBe(false);
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
		const result = await slackWebhook(store, {
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

	it("routes Slack DMs to the matching app-qualified subscription only", async () => {
		const store = createMockStorage();
		await store.addSubscription("slack:T123:app:A_BOBBERS", "bobbers");
		await store.addSubscription("slack:T123:app:A_ENG_TEAM", "eng-team");

		const result = await slackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A_BOBBERS",
			event: {
				type: "message",
				user: "U123",
				channel: "D456",
				channel_type: "im",
				text: "are you alive?",
				ts: "123",
			},
		});

		expect(result.status).toBe(200);
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered[0].topics).toEqual(["slack:T123:app:A_BOBBERS"]);
		expect(store.deliveredTo[0].ids).toEqual(["bobbers"]);
	});

	it("routes Slack channel events by app and channel when both are present", async () => {
		const store = createMockStorage();
		await store.addSubscription("slack:T123:app:A_ENG_TEAM:CENG", "eng-team");
		await store.addSubscription("slack:T123:app:A_BOBBERS:CENG", "bobbers");

		const result = await slackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A_ENG_TEAM",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "CENG",
				channel_type: "channel",
				text: "<@UENG> status?",
				ts: "123",
			},
		});

		expect(result.status).toBe(200);
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered[0].topics).toEqual([
			"slack:T123:app:A_ENG_TEAM",
			"slack:T123:app:A_ENG_TEAM:CENG",
		]);
		expect(store.deliveredTo[0].ids).toEqual(["eng-team"]);
	});

	it("does not deliver app-identified Slack events to stale legacy workspace subscriptions", async () => {
		const store = createMockStorage();
		await store.addSubscription("slack:T123", "bobbers-stale-legacy");
		await store.addSubscription("slack:T123:CENG", "bobbers-stale-channel");
		await store.addSubscription("slack:T123:app:A_ENG_TEAM:CENG", "eng-team");

		const result = await slackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A_ENG_TEAM",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "CENG",
				channel_type: "channel",
				text: "<@UENG> status?",
				ts: "123",
			},
		});

		expect(result.status).toBe(200);
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered[0].topics).toEqual([
			"slack:T123:app:A_ENG_TEAM",
			"slack:T123:app:A_ENG_TEAM:CENG",
		]);
		expect(store.deliveredTo[0].ids).toEqual(["eng-team"]);
	});

	it("returns ok for non-event_callback types without delivering", async () => {
		const store = createMockStorage();
		const result = await slackWebhook(store, { type: "app_rate_limited" });
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(0);
	});

	it("filters self-bot messages using stored workspace bot_id", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T123", { bot_token: "xoxb-test", bot_id: "BSELF" });

		const result = await slackWebhook(store, {
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

	// Cross-app loop (incident 2026-06-24, take 2): when two of OUR bots share a
	// channel, Slack delivers each bot's messages to BOTH apps' webhooks. A
	// message authored by bot A arrives via bot B's webhook (api_app_id = B), so a
	// self-filter keyed to the RECEIVING app (B) wouldn't recognise A as ours and
	// would deliver it → loop. It must be skipped because A is one of OUR bots.
	it("skips a message authored by any of our bots, even via another app's webhook", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T1", {
			bots: {
				A_old: { bot_token: "x1", bot_id: "B_old", app_id: "A_old" },
				A_new: { bot_token: "x2", bot_id: "B_new", app_id: "A_new" },
			},
		});
		// B_new authored it, but it arrives via A_old's webhook (api_app_id=A_old).
		const result = await slackWebhook(store, {
			type: "event_callback",
			team_id: "T1",
			api_app_id: "A_old",
			event: {
				type: "message",
				bot_id: "B_new",
				channel: "C1",
				channel_type: "channel",
				thread_ts: "100.000",
				text: "Evaluating…",
				ts: "123",
			},
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(0);
	});

	it("deduplicates app_mention against the matching message.channels event", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T123", {
			bots: {
				A123: {
					bot_token: "xoxb-test",
					bot_id: "BSELF",
					bot_user_id: "UBOT",
					app_id: "A123",
				},
			},
		});

		const mentionPayload = {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A123",
			event: {
				type: "app_mention",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "<@UBOT> can you check this?",
				ts: "123.456",
				thread_ts: "123.000",
			},
		};
		const messagePayload = {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A123",
			event: {
				type: "message",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "<@UBOT> can you check this?",
				ts: "123.456",
				thread_ts: "123.000",
			},
		};

		const mention = await slackWebhook(store, mentionPayload);
		const message = await slackWebhook(store, messagePayload);

		expect(mention.status).toBe(200);
		expect(message.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("slack.mention");
	});

	it("does not deduplicate a message mentioning another app's bot user", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T123", {
			bots: {
				A_ENG: {
					bot_token: "xoxb-eng",
					bot_id: "B_ENG",
					bot_user_id: "UENG",
					app_id: "A_ENG",
				},
				A_SUPPORT: {
					bot_token: "xoxb-support",
					bot_id: "B_SUPPORT",
					bot_user_id: "USUPPORT",
					app_id: "A_SUPPORT",
				},
			},
		});

		await slackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A_ENG",
			event: {
				type: "message",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "<@USUPPORT> can you check this?",
				ts: "123.456",
				thread_ts: "123.000",
			},
		});

		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("slack.thread_reply");
	});

	it("deduplicates with a single bot record even when api_app_id does not match its key", async () => {
		const store = createMockStorage();
		store.slackWorkspaces.set("T123", {
			bots: {
				BSELF: {
					bot_token: "xoxb-test",
					bot_id: "BSELF",
					bot_user_id: "UBOT",
				},
			},
		});

		await slackWebhook(store, {
			type: "event_callback",
			team_id: "T123",
			api_app_id: "A_UNKNOWN",
			event: {
				type: "message",
				user: "U123",
				channel: "C456",
				channel_type: "channel",
				text: "<@UBOT> can you check this?",
				ts: "123.456",
				thread_ts: "123.000",
			},
		});

		expect(store.delivered).toHaveLength(0);
	});

	it("returns challenge for url_verification payload", async () => {
		const store = createMockStorage();
		const result = await slackWebhook(store, {
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
			slackWebhook(store, {
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
		// MINT carries only non-global bootstrap topics (a freshly minted bubble
		// holds no resource grants yet; global topics arrive after authorize, #488).
		const body = { name: "my-deploy", subscriptions: ["_bootstrap", "inbox/my-deploy"] };
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
		// Non-global topics are namespaced to the minted bubble.
		expect(store.subscriptions.get(`${resp.bubble_id}:_bootstrap`)?.has(resp.deployment_id)).toBe(true);
		expect(store.initCalls).toHaveLength(1);
		expect(store.initCalls[0].deploymentId).toBe(resp.deployment_id);
	});

	it("indexes a granted global topic on JOIN; hard-rejects an ungranted one (#488)", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store);
		// Bubble holds a github grant but NOT a linear grant.
		store.seedGrant("github", "org/repo", bubble.id);

		// JOIN with only the granted topic — indexed (global topics are not namespaced).
		const okBody = { name: "d", subscriptions: ["github:org/repo"] };
		const okRaw = JSON.stringify(okBody);
		const okCtx = await signCtx(bubble, "POST", "/deployments", okRaw);
		const okRes = await handleRegisterDeployment(store, okBody, okCtx);
		expect(okRes.status).toBe(201);
		const okResp = okRes.body as { deployment_id: string };
		expect(store.subscriptions.get("github:org/repo")?.has(okResp.deployment_id)).toBe(true);

		// JOIN that also asks for the UNGRANTED linear topic — whole request is
		// rejected 400, listing the unauthorized topic, with NO index writes.
		const badBody = { name: "d2", subscriptions: ["inbox/d2", "github:org/repo", "linear:PROJ"] };
		const badRaw = JSON.stringify(badBody);
		const badCtx = await signCtx(bubble, "POST", "/deployments", badRaw, "n2");
		const badRes = await handleRegisterDeployment(store, badBody, badCtx);
		expect(badRes.status).toBe(400);
		expect((badRes.body as { error: string }).error).toBe("unauthorized_topics");
		expect((badRes.body as { topics: string[] }).topics).toEqual(["linear:PROJ"]);
		// Reject-as-a-unit: the non-global topic in the same request is NOT indexed.
		expect(store.subscriptions.get(`${bubble.id}:inbox/d2`)).toBeUndefined();
		expect(store.deployments.has("d2")).toBe(false);
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
		store.seedGrant("github", "org/repo", bubble.id); // re-register adds this global topic

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
		store.seedGrant("linear", "PROJ", "bub_test"); // grant required to add a global topic

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
		store.seedGrant("github", "org/repo", "bub_test");
		store.seedGrant("linear", "PROJ", "bub_test");

		const result = await handleUpdateSubscriptions(store, "d1", "key1", {
			add: ["github:org/repo", "linear:PROJ"],
		});
		const body = result.body as { subscriptions: string[]; added: number };
		expect(body.added).toBe(1);
		expect(body.subscriptions).toHaveLength(2);
	});

	it("replaces stale subscriptions and removes old index entries", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test",
			subscriptions: ["inbox/test", "slack:T1"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");
		store.seedGrant("slack", "T1", "bub_test");
		store.subscriptions.set("bub_test:inbox/test", new Set(["d1"]));
		store.subscriptions.set("slack:T1", new Set(["d1"]));

		const result = await handleUpdateSubscriptions(store, "d1", "key1", {
			replace: ["inbox/test", "slack:T1:app:A1"],
		});
		expect(result.status).toBe(200);
		const body = result.body as { subscriptions: string[]; added: number; removed: number };
		expect(body.subscriptions).toEqual(["inbox/test", "slack:T1:app:A1"]);
		expect(body.added).toBe(1);
		expect(body.removed).toBe(1);
		expect(store.subscriptions.get("slack:T1")).toBeUndefined();
		expect(store.subscriptions.get("slack:T1:app:A1")?.has("d1")).toBe(true);
	});

	it("rejects unauthorized requests", async () => {
		const store = createMockStorage();
		const result = await handleUpdateSubscriptions(store, "d1", "bad-key", { add: ["foo"] });
		expect(result.status).toBe(403);
	});

	it("rejects empty subscription updates", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test", subscriptions: [] };
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await handleUpdateSubscriptions(store, "d1", "key1", { add: [] });
		expect(result.status).toBe(400);
		const replaceResult = await handleUpdateSubscriptions(store, "d1", "key1", { replace: [] });
		expect(replaceResult.status).toBe(400);
	});

	it("persists updated deployment via putDeployment", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1", bubble_id: "bub_test",
			subscriptions: ["github:org/repo"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");
		store.seedGrant("slack", "T1", "bub_test");

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

	it("rejects body-derived global webhook routing fields on generic publishes", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store, "bub_A", "bkey_A");
		const body = { source: "ci", repo: "victim/repo", payload: { text: "fake" } };
		const raw = JSON.stringify(body);
		const ctx = await signCtx(bubble, "POST", "/events/deploy.complete", raw);
		const result = await handleTopicEvent(store, "deploy.complete", body, ctx);
		expect(result.status).toBe(400);
		expect(store.delivered).toHaveLength(0);
	});

	it("rejects source-derived global webhook topics on generic publishes", async () => {
		const store = createMockStorage();
		const bubble = seedBubble(store, "bub_A", "bkey_A");

		for (const [topic, body] of [
			["repo", { source: "github:org", payload: { text: "fake" } }],
			["issue", { source: "linear:TEAM", payload: { text: "fake" } }],
			["mention", { source: "slack:T123", payload: { text: "fake" } }],
			["github:org", { source: "ci", payload: { text: "fake" } }],
		] as const) {
			const raw = JSON.stringify(body);
			const ctx = await signCtx(bubble, "POST", `/events/${topic}`, raw);
			const result = await handleTopicEvent(store, topic, body, ctx);
			expect(result.status).toBe(400);
		}
		expect(store.delivered).toHaveLength(0);
	});
});

describe("handleSlackSend", () => {
	it("rejects missing channel", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { text: "hi", workspace: "T1" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("channel");
	});

	it("rejects missing text", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { channel: "C1", workspace: "T1" }, "bubA");
		expect(result.status).toBe(400);
	});

	it("rejects missing workspace", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { channel: "C1", text: "hi" }, "bubA");
		expect(result.status).toBe(400);
	});

	it("rejects unknown workspace", async () => {
		const store = createMockStorage();
		const result = await handleSlackSend(store, { channel: "C1", text: "hi", workspace: "T404" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("bot token");
	});

	// #487 isolation: outbound send reads ONLY the bubble-scoped record. A
	// workspace registered to bubble B must be invisible to bubble A, even
	// though both name the same Slack workspace id.
	it("does not read another bubble's workspace registration", async () => {
		const store = createMockStorage();
		stubSlackAuth("T1", "B_B", "A_B");
		// Bubble B registers workspace T1 (scoped to B).
		await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "xoxb-B", bot_id: "B_B" }, "bubB",
		);
		// Bubble A tries to send through T1 — it has no scoped record → 400.
		const result = await handleSlackSend(store, { channel: "C1", text: "hi", workspace: "T1" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("bot token");
	});

	// The global self-reply record (written for every registration) must NOT
	// satisfy outbound send — outbound must never fall back to global creds.
	it("does not fall back to the global workspace record", async () => {
		const store = createMockStorage();
		// Unsigned registration → only the global `T1` record exists, no scoped one.
		await handleSlackWorkspaceRegister(store, { workspace_id: "T1", bot_token: "xoxb-G", bot_id: "B_G" });
		expect(store.slackWorkspaces.get("T1")).toBeTruthy();        // global written
		expect(store.slackWorkspaces.get("bubA:T1")).toBeUndefined(); // no scoped record
		const result = await handleSlackSend(store, { channel: "C1", text: "hi", workspace: "T1" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("bot token");
	});
});

describe("handleChannelsSend", () => {
	// Stub fetch capturing Slack Web API calls; auth.test succeeds so a signed
	// workspace registration can seed the bubble-scoped token record. Send
	// traffic now flows through the Chat SDK api module, which form-encodes.
	function stubSlackApi(teamId: string) {
		const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/auth.test")) {
				return fetchOk(200, { ok: true, team_id: teamId, bot_id: "B1" });
			}
			if (u.includes("/bots.info")) {
				return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			}
			calls.push({ url: u, body: decodeSlackBody(init) });
			return fetchOk(200, { ok: true, ts: "99.1" });
		}));
		return calls;
	}

	async function seedWorkspace(store: ReturnType<typeof createMockStorage>, bubbleId: string) {
		await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "xoxb-A", bot_id: "B1" }, bubbleId,
		);
	}

	it("rejects missing conversation and missing text", async () => {
		const store = createMockStorage();
		let result = await handleChannelsSend(store, { text: "hi" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("conversation");
		result = await handleChannelsSend(store, { conversation: "slack:T1:dm:D1" }, "bubA");
		expect(result.status).toBe(400);
	});

	it("rejects a malformed conversation reference", async () => {
		const store = createMockStorage();
		const result = await handleChannelsSend(
			store, { conversation: "slack:T1:D1", text: "hi" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("invalid conversation");
	});

	// Regression: a non-string conversation must be a 400, not a TypeError
	// escaping the handler as a 500.
	it("rejects a non-string conversation with 400", async () => {
		const store = createMockStorage();
		for (const bad of [["slack", "T1", "dm", "D1"], 42, { ref: "x" }]) {
			const result = await handleChannelsSend(
				store, { conversation: bad, text: "hi" }, "bubA");
			expect(result.status).toBe(400);
		}
		const result = await handleChannelsSend(
			store, { conversation: "slack:T1:dm:D1", text: ["hi"] }, "bubA");
		expect(result.status).toBe(400);
	});

	it("rejects an unsupported channel", async () => {
		const store = createMockStorage();
		const result = await handleChannelsSend(
			store, { conversation: "telegram:12345:dm:67890", text: "hi" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("unsupported channel: telegram");
	});

	it("rejects an invalid mode and update without edit_ref", async () => {
		const store = createMockStorage();
		let result = await handleChannelsSend(
			store, { conversation: "slack:T1:dm:D1", text: "hi", mode: "stream" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("invalid mode");
		result = await handleChannelsSend(
			store, { conversation: "slack:T1:dm:D1", text: "hi", mode: "update" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("edit_ref");
	});

	// Same tenancy boundary as handleSlackSend: bubble-scoped lookup only.
	it("does not read another bubble's workspace registration", async () => {
		const store = createMockStorage();
		stubSlackApi("T1");
		await seedWorkspace(store, "bubB");
		const result = await handleChannelsSend(
			store, { conversation: "slack:T1:dm:D1", text: "hi" }, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("no send credential");
	});

	it("posts into the thread encoded in the conversation ref as native markdown", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34", text: "**hello**",
		}, "bubA");
		expect(result.status).toBe(200);
		expect((result.body as Record<string, unknown>).ts).toBe("99.1");
		expect(calls).toHaveLength(1);
		expect(calls[0].url).toContain("chat.postMessage");
		// Raw markdown goes out via markdown_text — formatting is the
		// gateway's job now, never the client's (#629).
		expect(calls[0].body).toMatchObject({
			channel: "C9", markdown_text: "**hello**", thread_ts: "12.34",
		});
		expect(calls[0].body.text).toBeUndefined();
	});

	it("posts without thread_ts when the ref has no thread", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:dm:D7", text: "yo",
		}, "bubA");
		expect(result.status).toBe(200);
		expect(calls[0].body.channel).toBe("D7");
		expect(calls[0].body.thread_ts).toBeUndefined();
	});

	it("mode update edits the message named by edit_ref and clears thread status", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			text: "final answer", mode: "update", edit_ref: "12.99",
		}, "bubA");
		expect(result.status).toBe(200);
		expect(calls).toHaveLength(2);
		expect(calls[0].url).toContain("chat.update");
		expect(calls[0].body).toMatchObject({ channel: "C9", ts: "12.99", markdown_text: "final answer" });
		// Replacing a placeholder clears the "is thinking..." indicator,
		// matching the CLI edit path.
		expect(calls[1].url).toContain("assistant.threads.setStatus");
		expect(calls[1].body).toMatchObject({ channel_id: "C9", thread_ts: "12.34", status: "" });
	});

	it("mode final posts when no edit_ref and clears thread status", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			text: "done", mode: "final",
		}, "bubA");
		expect(result.status).toBe(200);
		expect(calls).toHaveLength(2);
		expect(calls[0].url).toContain("chat.postMessage");
		expect(calls[1].url).toContain("assistant.threads.setStatus");
		expect(calls[1].body).toMatchObject({ status: "" });
	});

	it("mode final with edit_ref edits like update", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			text: "done", mode: "final", edit_ref: "12.99",
		}, "bubA");
		expect(result.status).toBe(200);
		expect(calls[0].url).toContain("chat.update");
		expect(calls[0].body).toMatchObject({ ts: "12.99" });
	});

	it("chunks an over-budget post into multiple sends, nothing truncated (#651)", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const long = Array.from({ length: 130 }, (_, i) => `paragraph ${i} ` + "x".repeat(100)).join("\n\n");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:dm:D7", text: long,
		}, "bubA");
		expect(result.status).toBe(200);
		const posts = calls.filter((c) => c.url.includes("chat.postMessage"));
		expect(posts.length).toBeGreaterThan(1);
		let joined = "";
		for (const p of posts) {
			const sent = String(p.body.markdown_text);
			// maxLength is the channel's HARD limit; every chunk must fit or
			// Slack rejects that send (msg_too_long).
			expect(sent.length).toBeLessThanOrEqual(12000);
			expect(sent).not.toContain("_(truncated)_");
			expect(p.body.channel).toBe("D7");
			joined += sent + "\n\n";
		}
		// Natural-boundary splits: the full text arrives across the posts.
		expect(joined).toContain("paragraph 0 ");
		expect(joined).toContain("paragraph 129 ");
	});

	it("returns the FIRST chunk's ts as the message identity", async () => {
		const store = createMockStorage();
		let n = 0;
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/auth.test")) return fetchOk(200, { ok: true, team_id: "T1", bot_id: "B1" });
			if (u.includes("/bots.info")) return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			void decodeSlackBody(init);
			n++;
			return fetchOk(200, { ok: true, ts: `100.${n}` });
		}));
		await seedWorkspace(store, "bubA");
		const long = Array.from({ length: 130 }, () => "y".repeat(100)).join("\n\n");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:dm:D7", text: long,
		}, "bubA");
		expect(result.status).toBe(200);
		expect((result.body as Record<string, unknown>).ts).toBe("100.1");
	});

	it("mode final with edit_ref chunks: placeholder edit first, follow-up posts after, one status clear", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const long = Array.from({ length: 130 }, (_, i) => `part ${i} ` + "z".repeat(100)).join("\n\n");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			text: long, mode: "final", edit_ref: "12.99",
		}, "bubA");
		expect(result.status).toBe(200);
		// First chunk replaces the placeholder, the rest post into the thread.
		expect(calls[0].url).toContain("chat.update");
		expect(calls[0].body).toMatchObject({ ts: "12.99" });
		const posts = calls.filter((c) => c.url.includes("chat.postMessage"));
		expect(posts.length).toBeGreaterThan(0);
		for (const p of posts) {
			expect(p.body.thread_ts).toBe("12.34");
			expect(String(p.body.markdown_text)).not.toContain("_(truncated)_");
		}
		// Typing/status cleared exactly once, after the last chunk.
		const status = calls.filter((c) => c.url.includes("assistant.threads.setStatus"));
		expect(status).toHaveLength(1);
		expect(calls[calls.length - 1].url).toContain("assistant.threads.setStatus");
	});

	it("mode update (streaming rewrite) still truncates instead of chunking", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const long = "x".repeat(13000);
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:dm:D7", text: long, mode: "update", edit_ref: "12.99",
		}, "bubA");
		expect(result.status).toBe(200);
		const updates = calls.filter((c) => c.url.includes("chat.update"));
		expect(updates).toHaveLength(1);
		const sent = String(updates[0].body.markdown_text);
		expect(sent.length).toBeLessThanOrEqual(12000);
		expect(sent.endsWith("_(truncated)_")).toBe(true);
		// No follow-up posts: a streaming tick must never multiply messages.
		expect(calls.filter((c) => c.url.includes("chat.postMessage"))).toHaveLength(0);
	});

	it("files with edit_ref replace the placeholder text, then attach", async () => {
		const store = createMockStorage();
		const calls: Array<{ url: string; body: string }> = [];
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/auth.test")) return fetchOk(200, { ok: true, team_id: "T1", bot_id: "B1" });
			if (u.includes("/bots.info")) return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			calls.push({ url: u, body: String(init?.body ?? "") });
			if (u.includes("getUploadURLExternal")) {
				return fetchOk(200, { ok: true, upload_url: "https://slack.test/upload", file_id: "F1" });
			}
			return fetchOk(200, { ok: true, ts: "12.99", files: [{ id: "F1" }] });
		}));
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			text: "done - see attached", mode: "final", edit_ref: "12.99",
			files: [{ name: "out.png", content_b64: btoa("png") }],
		}, "bubA");
		expect(result.status).toBe(200);
		const urls = calls.map((c) => c.url).join(" ");
		// Placeholder resolved first, then the file attached (no dup comment).
		expect(calls[0].url).toContain("chat.update");
		expect(String(calls[0].body)).toContain("12.99");
		expect(urls).toContain("completeUploadExternal");
	});

	it("rejects files with edit_ref but no text", async () => {
		const store = createMockStorage();
		stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			mode: "final", edit_ref: "12.99",
			files: [{ name: "out.png", content_b64: btoa("png") }],
		}, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("text required");
	});

	it("uploads files via the channel adapter", async () => {
		const store = createMockStorage();
		const calls: Array<{ url: string; body: string }> = [];
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/auth.test")) return fetchOk(200, { ok: true, team_id: "T1", bot_id: "B1" });
			if (u.includes("/bots.info")) return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			calls.push({ url: u, body: String(init?.body ?? "") });
			if (u.includes("getUploadURLExternal")) {
				return fetchOk(200, { ok: true, upload_url: "https://slack.test/upload", file_id: "F1" });
			}
			return fetchOk(200, { ok: true, files: [{ id: "F1" }] });
		}));
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:channel:C9:thread:12.34",
			text: "here you go",
			files: [{ name: "report.txt", content_b64: btoa("hello world"), title: "Report" }],
		}, "bubA");
		expect(result.status).toBe(200);
		const urls = calls.map((c) => c.url).join(" ");
		expect(urls).toContain("getUploadURLExternal");
		expect(urls).toContain("completeUploadExternal");
	});

	it("rejects malformed files", async () => {
		const store = createMockStorage();
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:dm:D1", files: [{ name: "x" }],
		}, "bubA");
		expect(result.status).toBe(400);
		expect((result.body as Record<string, string>).error).toContain("files");
	});

	it("surfaces a Slack API error as 502", async () => {
		const store = createMockStorage();
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL) => {
			const u = String(url);
			if (u.includes("/auth.test")) return fetchOk(200, { ok: true, team_id: "T1", bot_id: "B1" });
			if (u.includes("/bots.info")) return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			return fetchOk(200, { ok: false, error: "channel_not_found" });
		}));
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsSend(store, {
			conversation: "slack:T1:dm:D1", text: "hi",
		}, "bubA");
		expect(result.status).toBe(502);
		expect((result.body as Record<string, string>).error).toBe("channel_not_found");
	});
});

describe("handleChannelsTyping", () => {
	function stubSlackApi(teamId: string) {
		const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/auth.test")) return fetchOk(200, { ok: true, team_id: teamId, bot_id: "B1" });
			if (u.includes("/bots.info")) return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			calls.push({ url: u, body: decodeSlackBody(init) });
			return fetchOk(200, { ok: true });
		}));
		return calls;
	}

	async function seedWorkspace(store: ReturnType<typeof createMockStorage>, bubbleId: string) {
		await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "xoxb-A", bot_id: "B1" }, bubbleId,
		);
	}

	it("rejects missing conversation or non-boolean on", async () => {
		const store = createMockStorage();
		let result = await handleChannelsTyping(store, { on: true }, "bubA");
		expect(result.status).toBe(400);
		result = await handleChannelsTyping(store, { conversation: "slack:T1:dm:D1", on: "yes" }, "bubA");
		expect(result.status).toBe(400);
	});

	it("sets and clears the thread status", async () => {
		const store = createMockStorage();
		const calls = stubSlackApi("T1");
		await seedWorkspace(store, "bubA");
		let result = await handleChannelsTyping(store, {
			conversation: "slack:T1:channel:C9:thread:12.34", on: true,
		}, "bubA");
		expect(result.status).toBe(200);
		expect((result.body as Record<string, unknown>).supported).toBe(true);
		expect(calls[0].url).toContain("assistant.threads.setStatus");
		expect(calls[0].body).toMatchObject({
			channel_id: "C9", thread_ts: "12.34", status: "is thinking…",
		});
		result = await handleChannelsTyping(store, {
			conversation: "slack:T1:channel:C9:thread:12.34", on: false,
		}, "bubA");
		expect(result.status).toBe(200);
		expect(calls[1].body).toMatchObject({ status: "" });
	});

	it("keeps the tenancy boundary: no token for another bubble's workspace", async () => {
		const store = createMockStorage();
		stubSlackApi("T1");
		await seedWorkspace(store, "bubB");
		const result = await handleChannelsTyping(store, {
			conversation: "slack:T1:channel:C9:thread:12.34", on: true,
		}, "bubA");
		expect(result.status).toBe(400);
	});
});

describe("handleChannelsHistory", () => {
	function stubReplies(teamId: string, pages: Array<Record<string, unknown>>) {
		let page = 0;
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL) => {
			const u = String(url);
			if (u.includes("/auth.test")) return fetchOk(200, { ok: true, team_id: teamId, bot_id: "B1" });
			if (u.includes("/bots.info")) return fetchOk(200, { ok: true, bot: { app_id: "A1" } });
			return fetchOk(200, pages[Math.min(page++, pages.length - 1)]);
		}));
	}

	async function seedWorkspace(store: ReturnType<typeof createMockStorage>, bubbleId: string) {
		await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "xoxb-A", bot_id: "B1" }, bubbleId,
		);
	}

	it("returns the thread messages with file metadata", async () => {
		const store = createMockStorage();
		stubReplies("T1", [{
			ok: true,
			messages: [
				{ user: "U1", text: "question", ts: "12.34" },
				{
					user: "U2", text: "answer", ts: "12.35",
					files: [{ id: "F1", name: "a.png", mimetype: "image/png", url_private: "https://x" }],
				},
			],
		}]);
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsHistory(
			store, "slack:T1:channel:C9:thread:12.34", 100, "bubA");
		expect(result.status).toBe(200);
		const body = result.body as { messages: Array<Record<string, unknown>> };
		expect(body.messages).toHaveLength(2);
		expect(body.messages[0]).toMatchObject({ user: "U1", text: "question", ts: "12.34" });
		expect(body.messages[1].files).toEqual([
			{ id: "F1", name: "a.png", mimetype: "image/png", url_private: "https://x" },
		]);
	});

	it("reads channel history (oldest-first) for a ref without a thread anchor", async () => {
		const store = createMockStorage();
		// conversations.history returns newest-first; the gateway reverses it.
		stubReplies("T1", [{
			ok: true,
			messages: [
				{ user: "U2", text: "newest", ts: "2.0" },
				{ user: "U1", text: "oldest", ts: "1.0" },
			],
		}]);
		await seedWorkspace(store, "bubA");
		const result = await handleChannelsHistory(store, "slack:T1:dm:D1", 100, "bubA");
		expect(result.status).toBe(200);
		const body = result.body as { messages: Array<Record<string, unknown>> };
		expect(body.messages.map((m) => m.ts)).toEqual(["1.0", "2.0"]);
	});

	it("rejects missing/invalid conversation", async () => {
		const store = createMockStorage();
		let result = await handleChannelsHistory(store, "", 100, "bubA");
		expect(result.status).toBe(400);
		result = await handleChannelsHistory(store, "slack:T1:D1", 100, "bubA");
		expect(result.status).toBe(400);
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
			bot_user_id: "U_EXPLICIT",
		});
		expect(result.status).toBe(200);
		const body = result.body as Record<string, unknown>;
		expect(body.bot_id).toBe("B_EXPLICIT");
		expect(body.bot_user_id).toBe("U_EXPLICIT");
		const ws = store.slackWorkspaces.get("T_EXPLICIT");
		expect(ws?.bot_id).toBe("B_EXPLICIT");
		expect(ws?.bots?.B_EXPLICIT?.bot_user_id).toBe("U_EXPLICIT");
	});

	// #487: an UNSIGNED registration writes only the global self-reply record —
	// never a bubble-scoped one (it has no authenticated bubble).
	it("unsigned registration writes only the global record", async () => {
		const store = createMockStorage();
		await handleSlackWorkspaceRegister(store, {
			workspace_id: "T1", bot_token: "xoxb-1", bot_id: "B1", app_id: "A1",
		});
		expect(store.slackWorkspaces.get("T1")).toBeTruthy();
		// No bubble id was supplied → no scoped key for any bubble.
		expect([...store.slackWorkspaces.keys()]).toEqual(["T1"]);
	});

	// #487: a SIGNED registration writes BOTH the global record (inbound
	// self-reply) AND the bubble-scoped record (outbound send), enabling that
	// bubble — and only that bubble — to send through the workspace.
	it("signed registration writes both global and bubble-scoped records", async () => {
		const store = createMockStorage();
		stubSlackAuth("T1", "B_A", "A_A");
		await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "xoxb-A", bot_id: "B_A", app_id: "A_A" }, "bubA",
		);
		const global = store.slackWorkspaces.get("T1");
		const scoped = store.slackWorkspaces.get("bubA:T1");
		expect(global?.bots?.A_A?.bot_token).toBe("xoxb-A");
		expect(scoped?.bots?.A_A?.bot_token).toBe("xoxb-A");

		// The scoped record makes outbound send use the verified workspace token.
		const send = await handleSlackSend(
			store, { channel: "C1", text: "hi", workspace: "T1", app_id: "A_A" }, "bubA",
		);
		expect(send.status).toBe(200);
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
		const result = await slackWebhook(store, {
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

	// Regression: a re-registration that OMITS signing_secret (e.g. an older
	// client that doesn't send it) must NOT wipe a previously-stored secret —
	// otherwise every restart of that client drops the per-app secret and the
	// app's real events 401 against the global fallback.
	it("preserves an existing signing_secret when a later registration omits it", async () => {
		const store = createMockStorage();
		await handleSlackWorkspaceRegister(store, {
			workspace_id: "T1",
			bot_token: "x1",
			bot_id: "B1",
			bot_user_id: "U1",
			app_id: "A1",
			signing_secret: "sec-1",
		});
		// Older client re-registers the same app (same resolved app_id) WITHOUT a secret.
		await handleSlackWorkspaceRegister(store, {
			workspace_id: "T1", bot_token: "x1-rotated", bot_id: "B1", app_id: "A1",
		});
		const ws = store.slackWorkspaces.get("T1");
		expect(ws?.bots?.A1?.signing_secret).toBe("sec-1");      // preserved
		expect(ws?.bots?.A1?.bot_user_id).toBe("U1");            // preserved
		expect(ws?.bots?.A1?.bot_token).toBe("x1-rotated");      // token refreshed
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

// ---------------------------------------------------------------------------
// #488 — resource-grant authorization
// ---------------------------------------------------------------------------

describe("parseGlobalTopic / normalizeResource", () => {
	it("parses github + linear on the first colon", () => {
		expect(parseGlobalTopic("github:org/repo")).toEqual({ service: "github", resource: "org/repo" });
		expect(parseGlobalTopic("linear:ENG")).toEqual({ service: "linear", resource: "ENG" });
	});

	it("reduces a slack channel-scoped topic to the team id (grant gates the whole team)", () => {
		expect(parseGlobalTopic("slack:T123")).toEqual({ service: "slack", resource: "T123" });
		expect(parseGlobalTopic("slack:T123:C456")).toEqual({ service: "slack", resource: "T123" });
	});

	it("lowercases + strips .git for github so the grant key and topic never diverge", () => {
		expect(normalizeResource("github", "Org/Repo.git")).toBe("org/repo");
		expect(parseGlobalTopic("github:Org/Repo")).toEqual({ service: "github", resource: "org/repo" });
		// linear/slack are case-significant upstream — left verbatim.
		expect(normalizeResource("linear", "ENG")).toBe("ENG");
		expect(normalizeResource("slack", "T123")).toBe("T123");
	});

	it("returns null for non-global or malformed keys", () => {
		expect(parseGlobalTopic("inbox/manager")).toBeNull();
		expect(parseGlobalTopic("github:")).toBeNull();
		expect(parseGlobalTopic("monitor/support.email")).toBeNull();
	});
});

// A Response-ish stub for the verifier fetch (Worker/Node `fetch`).
function fetchOk(status: number, json: unknown = {}) {
	return { ok: status >= 200 && status < 300, status, json: async () => json };
}

// --- WhatsApp gateway (#656, epic #190 Phase 3) -----------------------------

const WA_PNID = "747556541";
const WA_USER = "15551234567";
const WA_CONV = `whatsapp:${WA_PNID}:dm:${WA_USER}`;

function metaWebhook(messages: Array<Record<string, unknown>>,
	extra: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		object: "whatsapp_business_account",
		entry: [{
			id: "WABA1",
			changes: [{
				field: "messages",
				value: {
					messaging_product: "whatsapp",
					metadata: { display_phone_number: "15550001111", phone_number_id: WA_PNID },
					contacts: [{ profile: { name: "Ada" }, wa_id: WA_USER }],
					messages,
					...extra,
				},
			}],
		}],
	};
}

describe("whatsapp webhook pipeline", () => {
	function req(rawBody: string, headers: Record<string, string> = {}): InboundWebhookRequest {
		const lower = Object.fromEntries(
			Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
		return { rawBody, header: (n) => lower[n.toLowerCase()] || "" };
	}

	it("GET handshake echoes the challenge only for the configured verify token", () => {
		const secrets = { whatsappVerifyToken: "vt-1" };
		const q = (params: Record<string, string>) => (n: string) => params[n] || "";

		const ok = handleWebhookHandshake("whatsapp", q({
			"hub.mode": "subscribe", "hub.verify_token": "vt-1", "hub.challenge": "12345",
		}), secrets);
		expect(ok).toEqual({ status: 200, text: "12345" });

		const wrong = handleWebhookHandshake("whatsapp", q({
			"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345",
		}), secrets);
		expect(wrong?.status).toBe(403);

		// Unset verify token REJECTS: echoing for anyone would let a third
		// party bind this URL to their Meta app.
		const unset = handleWebhookHandshake("whatsapp", q({
			"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "12345",
		}), {});
		expect(unset?.status).toBe(403);

		// Sources without a handshake fall through to the transport 404.
		expect(handleWebhookHandshake("github", q({}), secrets)).toBeNull();
	});

	it("verifies x-hub-signature-256 and delivers a normalized message", async () => {
		const store = createMockStorage();
		await store.addSubscription(`whatsapp:${WA_PNID}`, "sub1");
		const body = JSON.stringify(metaWebhook([{
			from: WA_USER, id: "wamid.A1", timestamp: "1783300000",
			type: "text", text: { body: "hello bobi" },
		}]));
		const secrets = { whatsapp: "app-secret" };

		const bad = await handleWebhookRequest(
			store, "whatsapp", req(body, { "x-hub-signature-256": "sha256=bad" }), secrets);
		expect(bad?.status).toBe(401);
		expect(store.delivered).toHaveLength(0);

		const sig = "sha256=" + (await hmacHex("app-secret", body));
		const ok = await handleWebhookRequest(
			store, "whatsapp", req(body, { "x-hub-signature-256": sig }), secrets);
		expect(ok?.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		const event = store.delivered[0];
		expect(event.source).toBe("whatsapp");
		expect(event.type).toBe("whatsapp.message");
		expect(event.id).toBe("wamid.A1");
		expect(event.topics).toEqual([`whatsapp:${WA_PNID}`]);
		expect(event.text).toBe("hello bobi");
		expect(event.conversation).toBe(WA_CONV);
		expect(event.fields?.profile_name).toBe("Ada");
	});

	it("records the message window per conversation and skips statuses", async () => {
		const store = createMockStorage();
		const receipts = metaWebhook([], {
			statuses: [{ id: "wamid.X", status: "delivered", recipient_id: WA_USER }],
		});
		let res = await handleWhatsAppWebhook(store, receipts);
		expect(res.status).toBe(200);
		expect(store.delivered).toHaveLength(0);
		expect(store.channelState.size).toBe(0);

		res = await handleWhatsAppWebhook(store, metaWebhook([{
			from: WA_USER, id: "wamid.A2", type: "text", text: { body: "hi" },
		}]));
		expect(res.status).toBe(200);
		const windowState = store.channelState.get(
			channelWindowKey("whatsapp", WA_PNID, WA_USER));
		expect(typeof windowState?.last_inbound).toBe("string");
	});

	it("surfaces media messages as typed placeholders with captions", async () => {
		const store = createMockStorage();
		await handleWhatsAppWebhook(store, metaWebhook([
			{ from: WA_USER, id: "wamid.I1", type: "image", image: { id: "m1", caption: "the chart" } },
			{ from: WA_USER, id: "wamid.V1", type: "voice", voice: { id: "m2" } },
		]));
		expect(store.delivered.map((e) => e.text)).toEqual(
			["[image] the chart", "[voice message]"]);
	});
});

describe("whatsapp number registration and outbound send", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		setWhatsAppApiUrl(undefined);
	});

	// Graph API stub: GET /<pnid>?fields=id verifies the token; POST
	// /<pnid>/messages records sends.
	function stubGraphApi(pnid: string) {
		const sends: Array<Record<string, unknown>> = [];
		vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes(`/${pnid}?fields=id`)) return fetchOk(200, { id: pnid });
			if (u.endsWith(`/${pnid}/messages`)) {
				sends.push(JSON.parse(String(init?.body)));
				return fetchOk(200, { messages: [{ id: `wamid.out.${sends.length}` }] });
			}
			return fetchOk(404, { error: { message: `unexpected ${u}` } });
		}));
		return sends;
	}

	async function register(store: ReturnType<typeof createMockStorage>, bubbleId = "bubA") {
		return handleWhatsAppNumberRegister(
			store, { phone_number_id: WA_PNID, access_token: "EAAG-token" }, bubbleId);
	}

	function openWindow(store: ReturnType<typeof createMockStorage>) {
		store.channelState.set(channelWindowKey("whatsapp", WA_PNID, WA_USER), {
			last_inbound: new Date().toISOString(),
		});
	}

	it("verifies the token upstream, stores the credential, writes the grant", async () => {
		const store = createMockStorage();
		stubGraphApi(WA_PNID);
		const res = await register(store);
		expect(res.status).toBe(200);
		expect(store.channelState.get(whatsappNumberKey("bubA", WA_PNID))).toEqual(
			{ access_token: "EAAG-token" });
		expect(await store.hasResourceGrant("whatsapp", WA_PNID, "bubA")).toBe(true);
	});

	it("rejects unsigned registration and a token the Graph API disowns", async () => {
		const store = createMockStorage();
		stubGraphApi(WA_PNID);
		expect((await register(store, "")).status).toBe(403);

		vi.stubGlobal("fetch", vi.fn(async () =>
			fetchOk(401, { error: { message: "bad token" } })));
		expect((await register(store)).status).toBe(403);
		expect(store.channelState.size).toBe(0);
	});

	it("sends inside the window through the Graph API and returns the message id", async () => {
		const store = createMockStorage();
		const sends = stubGraphApi(WA_PNID);
		await register(store);
		openWindow(store);

		const res = await handleChannelsSend(
			store, { conversation: WA_CONV, text: "reply *text*" }, "bubA");
		expect(res.status).toBe(200);
		expect((res.body as Record<string, unknown>).ts).toBe("wamid.out.1");
		expect(sends).toHaveLength(1);
		expect(sends[0]).toMatchObject({
			messaging_product: "whatsapp", to: WA_USER,
			type: "text", text: { body: "reply *text*" },
		});
	});

	it("mode final with edit_ref degrades to a post (no edit capability)", async () => {
		const store = createMockStorage();
		const sends = stubGraphApi(WA_PNID);
		await register(store);
		openWindow(store);

		const res = await handleChannelsSend(store, {
			conversation: WA_CONV, text: "final", mode: "final", edit_ref: "wamid.old",
		}, "bubA");
		expect(res.status).toBe(200);
		expect(sends).toHaveLength(1); // a new post, never an update call
	});

	it("returns the typed outside_message_window error for a KNOWN-stale window", async () => {
		const store = createMockStorage();
		stubGraphApi(WA_PNID);
		await register(store);

		// Stale window (25h old) - the helpful typed error.
		store.channelState.set(channelWindowKey("whatsapp", WA_PNID, WA_USER), {
			last_inbound: new Date(Date.now() - 25 * 3600_000).toISOString(),
		});
		const res = await handleChannelsSend(
			store, { conversation: WA_CONV, text: "hi" }, "bubA");
		expect(res.status).toBe(400);
		expect((res.body as Record<string, string>).error).toBe("outside_message_window");
	});

	it("a MISSING window record passes through (KV lag must not false-reject)", async () => {
		const store = createMockStorage();
		const sends = stubGraphApi(WA_PNID);
		await register(store);
		// No inbound recorded: the record may simply not have replicated yet
		// (Workers KV), and Meta is the authoritative window enforcer.
		const res = await handleChannelsSend(
			store, { conversation: WA_CONV, text: "hi" }, "bubA");
		expect(res.status).toBe(200);
		expect(sends).toHaveLength(1);
	});

	it("fails closed when no app secret is configured (unlike github/slack)", async () => {
		const store = createMockStorage();
		const body = JSON.stringify(metaWebhook([{
			from: WA_USER, id: "wamid.F1", type: "text", text: { body: "forged" },
		}]));
		const result = await handleWebhookRequest(
			store, "whatsapp",
			{ rawBody: body, header: () => "" },
			{}, // no whatsapp secret configured
		);
		expect(result?.status).toBe(401);
		expect(store.delivered).toHaveLength(0);
		expect(store.channelState.size).toBe(0); // no window state written
	});

	it("does not read another bubble's number registration", async () => {
		const store = createMockStorage();
		stubGraphApi(WA_PNID);
		await register(store, "bubB");
		openWindow(store);
		const res = await handleChannelsSend(
			store, { conversation: WA_CONV, text: "hi" }, "bubA");
		expect(res.status).toBe(400);
		expect((res.body as Record<string, string>).error).toContain("no send credential");
	});

	it("typing is a silent no-op and history is a clean 400", async () => {
		const store = createMockStorage();
		stubGraphApi(WA_PNID);
		await register(store);
		const typing = await handleChannelsTyping(
			store, { conversation: WA_CONV, on: true }, "bubA");
		expect(typing.status).toBe(200);
		expect((typing.body as Record<string, unknown>).supported).toBe(false);
		const history = await handleChannelsHistory(store, WA_CONV, 10, "bubA");
		expect(history.status).toBe(400);
	});
});

describe("handleAuthorizeResource", () => {
	afterEach(() => vi.unstubAllGlobals());

	it("github: a 2xx repo read writes a grant; the credential is never stored (test 2/4)", async () => {
		const store = createMockStorage();
		const fetchMock = vi.fn(async () => fetchOk(200, { full_name: "org/repo", private: true }));
		vi.stubGlobal("fetch", fetchMock);

		const res = await handleAuthorizeResource(
			store, { service: "github", resource: "org/repo", credential: "ghp_secrettoken" }, "bubA",
		);
		expect(res.status).toBe(200);
		expect(await store.hasResourceGrant("github", "org/repo", "bubA")).toBe(true);
		// The token was sent to GitHub but is never stored on the grant.
		const grant = store.resourceGrants.get("github:org/repo:bubA")!;
		expect(JSON.stringify(grant)).not.toContain("ghp_secrettoken");
		expect((grant as Record<string, unknown>).credential).toBeUndefined();
		expect(grant.account_id).toBeNull(); // shaped for the future account system
	});

	it("github: a 404/401 → opaque 403 and NO grant (test 2)", async () => {
		const store = createMockStorage();
		vi.stubGlobal("fetch", vi.fn(async () => fetchOk(404)));
		const res = await handleAuthorizeResource(
			store, { service: "github", resource: "org/ghost", credential: "ghp_x" }, "bubA",
		);
		expect(res.status).toBe(403);
		expect(await store.hasResourceGrant("github", "org/ghost", "bubA")).toBe(false);
	});

	it("github: 2xx grants regardless of private/permissions; non-2xx denies (Q4, test 14)", async () => {
		const store = createMockStorage();
		// A PUBLIC repo with permissions.push == false still grants — only the 2xx
		// distinction gates (the Rev-2 private||push tightening is dropped).
		vi.stubGlobal("fetch", vi.fn(async () =>
			fetchOk(200, { private: false, permissions: { push: false, pull: true } })));
		const ok = await handleAuthorizeResource(
			store, { service: "github", resource: "pub/repo", credential: "ghp_x" }, "bubA",
		);
		expect(ok.status).toBe(200);
		expect(await store.hasResourceGrant("github", "pub/repo", "bubA")).toBe(true);

		vi.stubGlobal("fetch", vi.fn(async () => fetchOk(403)));
		const denied = await handleAuthorizeResource(
			store, { service: "github", resource: "no/access", credential: "ghp_x" }, "bubA",
		);
		expect(denied.status).toBe(403);
		expect(await store.hasResourceGrant("github", "no/access", "bubA")).toBe(false);
	});

	it("linear: team key present → grant (records org id); absent → 403 (test 3)", async () => {
		const store = createMockStorage();
		vi.stubGlobal("fetch", vi.fn(async () =>
			fetchOk(200, { data: { teams: { nodes: [{ id: "t1", key: "ENG", organization: { id: "org_9" } }] } } })));
		const ok = await handleAuthorizeResource(
			store, { service: "linear", resource: "ENG", credential: "lin_secret" }, "bubA",
		);
		expect(ok.status).toBe(200);
		const grant = store.resourceGrants.get("linear:ENG:bubA")!;
		expect(grant.organization_id).toBe("org_9");
		expect(JSON.stringify(grant)).not.toContain("lin_secret");

		// A token valid org-wide but WITHOUT the requested team → no node → 403.
		vi.stubGlobal("fetch", vi.fn(async () => fetchOk(200, { data: { teams: { nodes: [] } } })));
		const denied = await handleAuthorizeResource(
			store, { service: "linear", resource: "OPS", credential: "lin_x" }, "bubA",
		);
		expect(denied.status).toBe(403);
		expect(await store.hasResourceGrant("linear", "OPS", "bubA")).toBe(false);
	});

	it("rejects invalid service / empty resource / missing credential with 400 (no upstream call)", async () => {
		const store = createMockStorage();
		const fetchMock = vi.fn();
		vi.stubGlobal("fetch", fetchMock);
		// slack is not authorized here (it converges via /slack/workspaces).
		expect((await handleAuthorizeResource(store, { service: "slack", resource: "T1", credential: "x" }, "b")).status).toBe(400);
		expect((await handleAuthorizeResource(store, { service: "github", resource: "", credential: "x" }, "b")).status).toBe(400);
		expect((await handleAuthorizeResource(store, { service: "github", resource: "org/repo", credential: "" }, "b")).status).toBe(400);
		// github must be owner/repo shape; linear must be alnum (GraphQL-injection guard).
		expect((await handleAuthorizeResource(store, { service: "github", resource: "not-a-slug", credential: "x" }, "b")).status).toBe(400);
		expect((await handleAuthorizeResource(store, { service: "linear", resource: 'A"}}}injection', credential: "x" }, "b")).status).toBe(400);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("is idempotent — re-authorizing re-verifies and is a no-op write", async () => {
		const store = createMockStorage();
		const fetchMock = vi.fn(async () => fetchOk(200, {}));
		vi.stubGlobal("fetch", fetchMock);
		await handleAuthorizeResource(store, { service: "github", resource: "org/repo", credential: "ghp_x" }, "bubA");
		await handleAuthorizeResource(store, { service: "github", resource: "org/repo", credential: "ghp_x" }, "bubA");
		expect(fetchMock).toHaveBeenCalledTimes(2);   // re-verifies each time
		expect(store.resourceGrants.size).toBe(1);     // single grant key
	});
});

describe("unauthorizedGlobalTopics", () => {
	it("returns only the global topics the bubble lacks a grant for", async () => {
		const store = createMockStorage();
		store.seedGrant("github", "org/repo", "bubA");
		const bad = await unauthorizedGlobalTopics(store, "bubA", [
			"inbox/manager",        // non-global — ignored
			"github:org/repo",      // granted — ok
			"linear:ENG",           // ungranted
			"slack:T1:C9",          // ungranted (team T1)
		]);
		expect(bad).toEqual(["linear:ENG", "slack:T1:C9"]);
	});
});

describe("resource-grant delivery filter (admittedDeploymentIds — tests 6/7)", () => {
	// Resolve admitted ids using the store's subscription index, the same way the
	// runtime adapters call admittedDeploymentIds.
	const admit = (store: ReturnType<typeof createMockStorage>, event: NormalizedEvent) =>
		admittedDeploymentIds(store, event, async (k) => store.subscriptions.get(k) ?? []);

	function depIn(store: ReturnType<typeof createMockStorage>, id: string, bubbleId: string) {
		store.deployments.set(id, { id, name: id, api_key: `k_${id}`, bubble_id: bubbleId, subscriptions: [] });
	}

	it("delivers a github event only to a bubble holding the grant — a stale index entry is dropped (test 6)", async () => {
		const store = createMockStorage();
		depIn(store, "depGranted", "bubGranted");
		depIn(store, "depStale", "bubStale");
		// BOTH are in the subscription index (depStale is a stale/forged entry).
		await store.addSubscription("github:org/repo", "depGranted");
		await store.addSubscription("github:org/repo", "depStale");
		store.seedGrant("github", "org/repo", "bubGranted"); // only the granted bubble

		const event = normalizeGitHubPayload("issues", "d1", {
			action: "opened", repository: { full_name: "org/repo" },
		})!;
		const admitted = await admit(store, event);
		expect([...admitted]).toEqual(["depGranted"]); // stale entry filtered out
	});

	it("multi-bubble: two granted bubbles both receive; a third without a grant gets none (test 7)", async () => {
		const store = createMockStorage();
		depIn(store, "depA", "bubA");
		depIn(store, "depB", "bubB");
		depIn(store, "depC", "bubC");
		for (const d of ["depA", "depB", "depC"]) await store.addSubscription("github:org/repo", d);
		store.seedGrant("github", "org/repo", "bubA");
		store.seedGrant("github", "org/repo", "bubB");

		const event = normalizeGitHubPayload("issues", "d2", {
			action: "opened", repository: { full_name: "org/repo" },
		})!;
		const admitted = await admit(store, event);
		expect([...admitted].sort()).toEqual(["depA", "depB"]);
	});

	it("slack: a channel event is grant-filtered by team id (slack:T:C → grant slack:T)", async () => {
		const store = createMockStorage();
		depIn(store, "depGranted", "bubGranted");
		depIn(store, "depStale", "bubStale");
		// The channel-scoped subscription key the manager registers.
		await store.addSubscription("slack:T1:C9", "depGranted");
		await store.addSubscription("slack:T1:C9", "depStale");
		store.seedGrant("slack", "T1", "bubGranted"); // grant keyed on the TEAM id

		const event = bridgeSlackWebhook(JSON.stringify({
			type: "event_callback", team_id: "T1",
			event: { type: "app_mention", channel: "C9", channel_type: "channel", user: "U1", text: "hi", ts: "1.0" },
		})).event!;
		const admitted = await admit(store, event);
		expect([...admitted]).toEqual(["depGranted"]);
	});

	it("a non-global (bubble-scoped) event admits all subscribers without a grant check", async () => {
		const store = createMockStorage();
		depIn(store, "dep1", "bubA");
		await store.addSubscription(namespaceSubKey("bubA", "inbox/x"), "dep1");
		const event = createTopicEvent("inbox/x", { payload: {} }, "bubA");
		const admitted = await admit(store, event);
		expect([...admitted]).toEqual(["dep1"]); // no grant needed
	});
});

describe("handleSlackWorkspaceRegister resource grant (#488 §6, test 8)", () => {
	it("a SIGNED registration writes a slack grant; an UNSIGNED one does not", async () => {
		const store = createMockStorage();
		// Unsigned (no bubble) — global self-reply record only, no grant.
		await handleSlackWorkspaceRegister(store, { workspace_id: "T1", bot_token: "x", bot_id: "B1", app_id: "A1" });
		expect(await store.hasResourceGrant("slack", "T1", "bubA")).toBe(false);

		// Signed (bubble authenticated) — also writes the slack grant.
		stubSlackAuth("T1", "B1", "A1");
		await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "x", bot_id: "B1", app_id: "A1" }, "bubA",
		);
		expect(await store.hasResourceGrant("slack", "T1", "bubA")).toBe(true);
	});

	it("denies a signed slack registration when auth.test cannot prove the workspace", async () => {
		const store = createMockStorage();
		vi.stubGlobal("fetch", vi.fn(async () => fetchOk(200, { ok: true, team_id: "T_OTHER", bot_id: "B1" })));

		const result = await handleSlackWorkspaceRegister(
			store, { workspace_id: "T1", bot_token: "x", bot_id: "B_FAKE", app_id: "A_FAKE" }, "bubA",
		);

		expect(result.status).toBe(403);
		expect(await store.hasResourceGrant("slack", "T1", "bubA")).toBe(false);
		expect(store.slackWorkspaces.get("bubA:T1")).toBeUndefined();
	});
});
