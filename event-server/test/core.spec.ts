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
	it("normalizes and delivers a v2 github event", async () => {
		const store = createMockStorage();
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
		expect((result.body as Record<string, number>).delivered_to).toBe(1);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].delivery).toBe("chat");
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
	it("creates and delivers a v2 topic event", async () => {
		const store = createMockStorage();
		const result = await handleTopicEvent(store, "deploy.complete", {
			source: "ci",
			payload: { sha: "abc" },
		});
		expect(result.status).toBe(200);
		expect(store.delivered).toHaveLength(1);
		expect(store.delivered[0].type).toBe("deploy.complete");
		expect(store.delivered[0].v).toBe(2);
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
});
