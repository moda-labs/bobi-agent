import { describe, it, expect } from "vitest";
import {
	type StorageAdapter,
	type DeploymentRecord,
	type SlackWorkspaceRecord,
	type NormalizedEvent,
	createTopicEvent,
	normalizeGitHubPayload,
	normalizeLinearPayload,
	normalizeSlackPayload,
	subscriptionKeysForEvent,
	verifyGitHubSignature,
	authenticateDeployment,
	handleGitHubWebhook,
	handleLinearWebhook,
	handleSlackWebhook,
	handleRegisterDeployment,
	handleUpdateSubscriptions,
	handleTopicEvent,
	handleSlackSend,
	handleSlackWorkspaceRegister,
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

describe("subscriptionKeysForEvent", () => {
	it("returns only repo key for github events (no type fallback)", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "github",
			type: "github.issues",
			repo: "org/repo",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual(["github:org/repo"]);
	});

	it("returns only linear key for linear events (no type fallback)", () => {
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

	it("returns only workspace key for slack events (no type fallback)", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "slack",
			type: "slack.mention",
			workspace: "T123",
			channel: "C456",
			timestamp: "",
			payload: {},
		});
		expect(keys).toEqual(["slack:T123"]);
	});

	it("returns type as fallback key when no source-specific routing fields", () => {
		const keys = subscriptionKeysForEvent({
			id: "1",
			source: "unknown",
			type: "test",
			timestamp: "",
			payload: {},
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

// ---------------------------------------------------------------------------
// Mock storage adapter for handler tests
// ---------------------------------------------------------------------------

function createMockStorage(): StorageAdapter & {
	deployments: Map<string, DeploymentRecord>;
	apiKeyIndex: Map<string, string>;
	subscriptions: Map<string, Set<string>>;
	slackWorkspaces: Map<string, SlackWorkspaceRecord>;
	delivered: NormalizedEvent[];
	initCalls: Array<{ deploymentId: string; subscriptions: string[] }>;
} {
	const deployments = new Map<string, DeploymentRecord>();
	const apiKeyIndex = new Map<string, string>();
	const subscriptions = new Map<string, Set<string>>();
	const slackWorkspaces = new Map<string, SlackWorkspaceRecord>();
	const delivered: NormalizedEvent[] = [];
	const initCalls: Array<{ deploymentId: string; subscriptions: string[] }> = [];

	return {
		deployments,
		apiKeyIndex,
		subscriptions,
		slackWorkspaces,
		delivered,
		initCalls,

		async getDeploymentByApiKey(apiKey: string) {
			const id = apiKeyIndex.get(apiKey);
			if (!id) return null;
			return deployments.get(id) || null;
		},
		async putDeployment(dep: DeploymentRecord) {
			deployments.set(dep.id, { ...dep });
			apiKeyIndex.set(dep.api_key, dep.id);
		},
		async addSubscription(key: string, deploymentId: string) {
			if (!subscriptions.has(key)) subscriptions.set(key, new Set());
			subscriptions.get(key)!.add(deploymentId);
		},
		async deliver(event: NormalizedEvent) {
			delivered.push(event);
			return 1;
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

// ---------------------------------------------------------------------------
// Handler tests
// ---------------------------------------------------------------------------

describe("authenticateDeployment", () => {
	it("returns deployment when api key and id match", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", subscriptions: [] };
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
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", subscriptions: [] };
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		expect(await authenticateDeployment(store, "key1", "wrong-id")).toBeNull();
	});
});

describe("handleGitHubWebhook", () => {
	it("normalizes and delivers a github event", async () => {
		const store = createMockStorage();
		const result = await handleGitHubWebhook(store, "issues", "del-1", {
			action: "opened",
			repository: { full_name: "org/repo" },
		});
		expect(result.status).toBe(200);
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("github.issues");
	});

	it("returns 400 for payload without repository", async () => {
		const store = createMockStorage();
		const result = await handleGitHubWebhook(store, "push", "del-1", { action: "opened" });
		expect(result.status).toBe(400);
	});
});

describe("handleLinearWebhook", () => {
	it("normalizes and delivers a linear event", async () => {
		const store = createMockStorage();
		const result = await handleLinearWebhook(store, {
			action: "update",
			type: "Issue",
			data: { team: { key: "PROJ" } },
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("linear.Issue.update");
	});
});

describe("handleSlackWebhook", () => {
	it("delivers a slack mention event", async () => {
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
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered).toHaveLength(1);
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
});

describe("handleRegisterDeployment", () => {
	it("creates a deployment with subscriptions and calls initDeploymentSession", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, {
			name: "my-deploy",
			subscriptions: ["github:org/repo", "linear:PROJ"],
		});
		expect(result.status).toBe(201);
		const body = result.body as { deployment_id: string; api_key: string };
		expect(body.deployment_id).toBeTruthy();
		expect(body.api_key).toMatch(/^moda_/);

		expect(store.deployments.size).toBe(1);
		expect(store.subscriptions.get("github:org/repo")?.has(body.deployment_id)).toBe(true);
		expect(store.subscriptions.get("linear:PROJ")?.has(body.deployment_id)).toBe(true);
		expect(store.initCalls).toHaveLength(1);
		expect(store.initCalls[0].deploymentId).toBe(body.deployment_id);
	});

	it("rejects missing name", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, { subscriptions: ["foo"] });
		expect(result.status).toBe(400);
	});

	it("rejects missing subscriptions", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, { name: "test" });
		expect(result.status).toBe(400);
	});

	it("rejects empty subscriptions array", async () => {
		const store = createMockStorage();
		const result = await handleRegisterDeployment(store, { name: "test", subscriptions: [] });
		expect(result.status).toBe(400);
	});
});

describe("handleUpdateSubscriptions", () => {
	it("adds new subscriptions to an existing deployment", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1",
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

	it("deduplicates existing subscriptions", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1",
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
		const dep: DeploymentRecord = { id: "d1", name: "test", api_key: "key1", subscriptions: [] };
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		const result = await handleUpdateSubscriptions(store, "d1", "key1", { add: [] });
		expect(result.status).toBe(400);
	});

	it("persists updated deployment via putDeployment", async () => {
		const store = createMockStorage();
		const dep: DeploymentRecord = {
			id: "d1", name: "test", api_key: "key1",
			subscriptions: ["github:org/repo"],
		};
		store.deployments.set("d1", dep);
		store.apiKeyIndex.set("key1", "d1");

		await handleUpdateSubscriptions(store, "d1", "key1", { add: ["slack:T1"] });
		const saved = store.deployments.get("d1")!;
		expect(saved.subscriptions).toContain("slack:T1");
	});
});

describe("handleTopicEvent", () => {
	it("creates and delivers a topic event", async () => {
		const store = createMockStorage();
		const result = await handleTopicEvent(store, "deploy.complete", {
			source: "ci",
			payload: { sha: "abc" },
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("deploy.complete");
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
});
