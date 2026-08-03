import { SELF, env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";

import capture from "./fixtures-claude-code-mcp.json";
import { MCP_SERVER_NAME, MCP_SERVER_VERSION } from "../src/mcp";

// ---------------------------------------------------------------------------
// The MCP control surface (/mcp).
//
// Two things are proven here, and they need different kinds of test.
//
// AUTHORIZATION is proven directly: /mcp is a new public route on a Worker that
// also serves the bus, so an unauthenticated request must be rejected before
// any tool body runs. Those tests fail on the mutant that drops the
// requireOperator call in index.ts.
//
// CONFORMANCE is proven by REPLAY. `fixtures-claude-code-mcp.json` is not
// hand-written from the spec - it is the literal request bodies a real
// claude-code/2.1.220 session sent through a logging proxy at a live
// `wrangler dev`, captured while it listed the tools and called all three
// (including both not-found paths). Asserting against those bytes means the
// suite fails when a real client would fail, rather than when our reading of
// the spec would. Re-capture it the same way if the surface changes; see
// plans/2026-07-30-mcp-fleet-control.md "Proof of work".
// ---------------------------------------------------------------------------

const OPERATOR = "test-operator-token";
const MCP_URL = "https://example.com/mcp";

interface CapturedRequest {
	method: string;
	id: number | null;
	body: string;
	headers: Record<string, string | null>;
}

const captured = capture as {
	captured_from: string;
	negotiated_protocol_version: string;
	requests: CapturedRequest[];
};

function findCaptured(method: string, predicate?: (body: string) => boolean): CapturedRequest {
	const match = captured.requests.find(
		(r) => r.method === method && (!predicate || predicate(r.body)),
	);
	// A fixture that silently lost an entry would turn every assertion below
	// into a vacuous pass, so a missing capture is a hard failure.
	if (!match) throw new Error(`fixture has no captured ${method} request`);
	return match;
}

/** Replay one captured request verbatim, with only the bearer swapped in. */
function replay(req: CapturedRequest, token: string | null = OPERATOR): Promise<Response> {
	const headers: Record<string, string> = { "content-type": "application/json" };
	for (const [k, v] of Object.entries(req.headers)) {
		if (v !== null && v !== undefined) headers[k] = v;
	}
	if (token !== null) headers.authorization = `Bearer ${token}`;
	return SELF.fetch(MCP_URL, { method: "POST", headers, body: req.body });
}

/**
 * Read a JSON-RPC result out of a response.
 *
 * The transport answers on `text/event-stream`, so the JSON-RPC message is the
 * `data:` line of an SSE frame rather than the whole body.
 */
async function rpcResult(res: Response): Promise<Record<string, unknown>> {
	const text = await res.text();
	if (res.headers.get("content-type")?.includes("text/event-stream")) {
		const line = text.split("\n").find((l) => l.startsWith("data:"));
		if (!line) throw new Error(`no SSE data frame in response: ${text}`);
		return JSON.parse(line.slice("data:".length).trim());
	}
	return JSON.parse(text);
}

/** The text payload of a tools/call result. */
async function toolText(res: Response): Promise<string> {
	const msg = await rpcResult(res);
	const result = msg.result as { content?: { type: string; text: string }[] } | undefined;
	if (!result) throw new Error(`tools/call returned no result: ${JSON.stringify(msg)}`);
	return (result.content ?? []).map((c) => c.text).join("");
}

async function toolResult(res: Response): Promise<{ isError?: boolean; content: { text: string }[] }> {
	const msg = await rpcResult(res);
	return msg.result as { isError?: boolean; content: { text: string }[] };
}

// The instance the captured session read. Seeded straight into KV: the
// heartbeat publish path is fleet.spec.ts's subject, not this suite's.
const SEEDED_INSTANCE = {
	received_at: 0, // rewritten per test so reachability is deterministic
	bubble_id: "moda",
	snapshot: {
		deployment: { fleet: "moda", instance: "eng-team" },
		generated_at: 0,
		manager: { state: "running", pid: 4242, restart_count: 3 },
		sessions: [
			{ name: "engineer", state: "running" },
			{ name: "reviewer", state: "idle" },
		],
		versions: { bobi: "0.51.1", team: "eng-team@2.4.0" },
	},
};

/**
 * Does this bobi_fleet_status entry describe the instance this suite seeds?
 *
 * The pool runs with `isolatedStorage: false` (see vitest.config.mts), so KV is
 * shared with the other spec files - fleet.spec.ts publishes real heartbeats
 * that land in the same read model. Assertions here therefore address the
 * seeded instance directly and never the size of the fleet, which is not this
 * suite's to know.
 */
function isSeeded(entry: { deployment?: { fleet?: string; instance?: string } }): boolean {
	return entry.deployment?.fleet === "moda" && entry.deployment?.instance === "eng-team";
}

async function seedInstance(): Promise<void> {
	const now = Date.now();
	const record = {
		...SEEDED_INSTANCE,
		received_at: now,
		snapshot: { ...SEEDED_INSTANCE.snapshot, generated_at: now },
	};
	await env.EVENTS.put("fleet_instance:moda:eng-team", JSON.stringify(record));
}

beforeEach(async () => {
	await env.EVENTS.delete("fleet_instance:moda:eng-team");
});

// ---------------------------------------------------------------------------

describe("/mcp authorization", () => {
	// The route is reachable by anyone who can reach the bus. Every one of these
	// must reject BEFORE a tool body runs - not merely return an error result.
	const initialize = () => findCaptured("initialize");

	it("rejects a request with no Authorization header", async () => {
		const res = await replay(initialize(), null);
		expect(res.status).toBe(401);
		expect(await res.json()).toEqual({ error: "unauthorized" });
	});

	it("rejects a wrong bearer token", async () => {
		const res = await replay(initialize(), "not-the-operator-token");
		expect(res.status).toBe(401);
	});

	it("rejects a token that is a prefix of the real one", async () => {
		const res = await replay(initialize(), OPERATOR.slice(0, -1));
		expect(res.status).toBe(401);
	});

	it("rejects an unauthenticated tools/call without running the tool", async () => {
		await seedInstance();
		const res = await replay(findCaptured("tools/call", (b) => b.includes("bobi_fleet_status")), null);
		expect(res.status).toBe(401);
		// The 401 body is the operator gate's, not a JSON-RPC result: proof the
		// request never reached the MCP handler at all.
		expect(await res.json()).toEqual({ error: "unauthorized" });
	});

	it("accepts the operator token", async () => {
		const res = await replay(initialize());
		expect(res.status).toBe(200);
	});
});

describe("/mcp conformance against captured claude-code traffic", () => {
	it("completes the captured initialize handshake", async () => {
		const res = await replay(findCaptured("initialize"));
		expect(res.status).toBe(200);

		const msg = await rpcResult(res);
		const result = msg.result as Record<string, unknown>;
		expect(msg.jsonrpc).toBe("2.0");
		// The id is echoed from the captured request - claude-code starts at 0,
		// which a truthiness bug in id handling would drop.
		expect(msg.id).toBe(findCaptured("initialize").id);
		expect(result.serverInfo).toEqual({ name: MCP_SERVER_NAME, version: MCP_SERVER_VERSION });
		expect(result.protocolVersion).toBe(captured.negotiated_protocol_version);
		expect((result.capabilities as Record<string, unknown>).tools).toBeTruthy();
		// The instructions are what orients an agent before its first call; an
		// empty string would still be a valid handshake and a useless surface.
		expect(String(result.instructions)).toContain("bobi_fleet_status");
	});

	it("accepts the captured initialized notification", async () => {
		const res = await replay(findCaptured("notifications/initialized"));
		// A notification carries no id, so the transport answers 202 with no body.
		expect(res.status).toBe(202);
	});

	it("lists exactly the three read tools, with schemas", async () => {
		const res = await replay(findCaptured("tools/list"));
		const msg = await rpcResult(res);
		const tools = (msg.result as { tools: { name: string; description?: string; inputSchema?: Record<string, unknown> }[] }).tools;

		expect(tools.map((t) => t.name).sort()).toEqual([
			"bobi_command_result",
			"bobi_fleet_status",
			"bobi_instance_detail",
		]);

		for (const tool of tools) {
			// A tool whose description is missing is unusable by an agent that has
			// never seen the fleet, which is the entire point of this surface.
			expect(tool.description, `${tool.name} has no description`).toBeTruthy();
			expect(tool.inputSchema, `${tool.name} advertises no input schema`).toBeTruthy();
		}

		// zod schemas must reach the wire as JSON Schema, with the required
		// arguments named - this is what stops an agent guessing argument names.
		const detail = tools.find((t) => t.name === "bobi_instance_detail")!;
		expect(detail.inputSchema).toMatchObject({ type: "object" });
		expect((detail.inputSchema as { required?: string[] }).required?.sort()).toEqual(["fleet", "instance"]);

		const command = tools.find((t) => t.name === "bobi_command_result")!;
		expect((command.inputSchema as { required?: string[] }).required?.sort()).toEqual([
			"command_id",
			"fleet",
			"instance",
		]);

		// bobi_fleet_status takes no arguments; an accidental required field would
		// make the orienting read uncallable.
		const status = tools.find((t) => t.name === "bobi_fleet_status")!;
		expect((status.inputSchema as { required?: string[] }).required ?? []).toEqual([]);
	});
});

describe("/mcp read tools", () => {
	it("bobi_fleet_status returns the fleet read model", async () => {
		await seedInstance();
		const res = await replay(findCaptured("tools/call", (b) => b.includes("bobi_fleet_status")));
		const body = JSON.parse(await toolText(res));

		expect(body.instances.find(isSeeded)).toMatchObject({
			deployment: { fleet: "moda", instance: "eng-team" },
			reachability: "live",
			manager: { state: "running", restart_count: 3 },
			versions: { bobi: "0.51.1" },
		});
	});

	it("bobi_fleet_status reports nothing for an instance with no heartbeat", async () => {
		// beforeEach removed the seed, so this is the never-seen case: a
		// well-formed empty answer, not an error and not a placeholder entry.
		const res = await replay(findCaptured("tools/call", (b) => b.includes("bobi_fleet_status")));
		const result = await toolResult(res);
		expect(result.isError).toBeFalsy();

		const body = JSON.parse(result.content[0].text);
		expect(Array.isArray(body.instances)).toBe(true);
		expect(body.instances.find(isSeeded)).toBeUndefined();
	});

	it("bobi_instance_detail returns the heartbeat plus the lifecycle trail", async () => {
		await seedInstance();
		const res = await replay(
			findCaptured("tools/call", (b) => b.includes("bobi_instance_detail") && b.includes("eng-team")),
		);
		const body = JSON.parse(await toolText(res));

		expect(body).toMatchObject({
			deployment: { fleet: "moda", instance: "eng-team" },
			reachability: "live",
			sessions: [
				{ name: "engineer", state: "running" },
				{ name: "reviewer", state: "idle" },
			],
		});
		expect(body.lifecycle).toEqual([]);
	});

	it("bobi_instance_detail on an unknown instance is a tool error naming the recovery", async () => {
		const res = await replay(
			findCaptured("tools/call", (b) => b.includes("bobi_instance_detail") && b.includes("nope")),
		);
		const result = await toolResult(res);

		// isError, not a thrown protocol error: the agent should be able to
		// correct itself and call again rather than lose the connection.
		expect(result.isError).toBe(true);
		expect(result.content[0].text).toContain("bobi_fleet_status");
	});

	it("bobi_command_result on an unknown command id is a tool error", async () => {
		await seedInstance();
		const res = await replay(findCaptured("tools/call", (b) => b.includes("bobi_command_result")));
		const result = await toolResult(res);

		expect(result.isError).toBe(true);
		expect(result.content[0].text).toContain("missing-id");
	});

	it("bobi_command_result reads back a folded command", async () => {
		await seedInstance();
		await env.EVENTS.put(
			"fleet_command:moda:eng-team:cmd-1",
			JSON.stringify({
				command_id: "cmd-1",
				fleet: "moda",
				instance: "eng-team",
				command: "status",
				issued_at: Date.now(),
			}),
		);
		await env.EVENTS.put(
			"fleet_command_result:moda:eng-team:cmd-1",
			JSON.stringify({
				command_id: "cmd-1",
				status: "done",
				result: { ok: true },
				completed_at: Date.now(),
			}),
		);

		const captureBody = findCaptured("tools/call", (b) => b.includes("bobi_command_result"));
		const res = await replay({ ...captureBody, body: captureBody.body.replace("missing-id", "cmd-1") });
		const body = JSON.parse(await toolText(res));

		expect(body).toMatchObject({
			command_id: "cmd-1",
			command: "status",
			status: "done",
			result: { ok: true },
		});
	});

	it("rejects arguments that do not match the declared schema", async () => {
		const captureBody = findCaptured("tools/call", (b) => b.includes("bobi_instance_detail") && b.includes("eng-team"));
		// Empty fleet name - the schema declares min(1), so the SDK must reject it
		// rather than let it reach a KV read for `fleet_instance::eng-team`.
		const res = await replay({ ...captureBody, body: captureBody.body.replace('"fleet":"moda"', '"fleet":""') });
		const msg = await rpcResult(res);
		const failed =
			msg.error !== undefined || (msg.result as { isError?: boolean } | undefined)?.isError === true;
		expect(failed, `expected a schema rejection, got ${JSON.stringify(msg)}`).toBe(true);
	});
});
