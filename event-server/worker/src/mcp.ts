// ---------------------------------------------------------------------------
// The agentic control surface: MCP over the fleet read model.
//
// `/fleet/*` is a fine HTTP API and a poor tool surface - a consumer has to be
// told the URL shape, the 202-then-poll contract, and the per-command argument
// shapes before it can do anything. This module is the same control plane with
// the vocabulary shaped for a reader who has never seen the fleet: named tools,
// declared schemas, and descriptions that say what each result actually means.
//
// It is a schema wrapper, not a second implementation. Every tool calls the
// same builder the corresponding HTTP route calls, in-process against
// `createFleetKVStorage(env.EVENTS)` - no HTTP hop, and one place where the
// read model lives.
//
// Statelessness is the design, not an accident: `createMcpHandler` builds one
// `McpServer` per request over a web-standards transport, with no Durable
// Object and no session id. Fleet state lives in KV, where the console already
// reads it, so there is nothing for a session to hold.
//
// Phase 1 registers the read half only. The write tools (`bobi_read_transcript`,
// `bobi_send_message`, `bobi_lifecycle`) and the bounded server-side wait
// arrive in Lane B; see plans/2026-07-30-mcp-fleet-control.md.
// ---------------------------------------------------------------------------

import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

import { type FleetStorage, buildCommandView, buildFleetStatus, buildInstanceDetail, createFleetKVStorage, windowsFromEnv } from "./fleet";

// The slice of the Worker env the MCP surface reads. Declared structurally
// rather than importing index.ts's `Env`, which would be a cycle - index.ts
// imports this module.
export interface McpEnv {
	EVENTS: KVNamespace;
	FLEET_LIVE_WINDOW_S?: string;
	FLEET_STALE_WINDOW_S?: string;
}

// Server identity as it appears in the client's `initialize` result. The
// version is the MCP surface's own contract version, deliberately not the
// Worker release: a client caches tool schemas against it, so it moves when the
// tool surface changes, not when the Worker deploys.
export const MCP_SERVER_NAME = "bobi-fleet";
export const MCP_SERVER_VERSION = "1.0.0";

// The route the handler owns. Exported so index.ts routes on the same literal
// the handler is configured with, rather than two copies that can drift.
export const MCP_ROUTE = "/mcp";

// What an agent reads before it calls anything. `instructions` is served in the
// `initialize` result, so this is the one place to say what the surface is for
// and what it deliberately cannot do.
const SERVER_INSTRUCTIONS = `Control plane for a Bobi agent fleet.

Each instance is one deployed agent team, addressed by a (fleet, instance)
pair. Start with bobi_fleet_status - it is the orienting read and needs no
arguments - then drill into a specific instance with bobi_instance_detail.

Reachability ("live" / "stale" / "unreachable") is derived from how recently
the instance's supervisor sent a heartbeat, not from a probe issued now. An
instance that has never sent a heartbeat does not appear at all.

This phase is read-only. Sending messages, reading transcripts, and lifecycle
control (restart/stop/start) are not yet exposed as tools.`;

// Tool results are JSON text. `structuredContent` is deliberately not used:
// the spec pairs it with a declared `outputSchema`, and pinning an output
// schema here would freeze the read model's shape into the tool contract -
// the read model is owned by fleet.ts and the supervisor's heartbeat, and it
// grows additively. Text keeps the tool contract to the argument shape.
function jsonResult(value: unknown) {
	return { content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }] };
}

// A tool failure the agent can act on (unknown instance, unknown command id) -
// `isError` so the client renders it as a failed call rather than data, with a
// message that says what to do next instead of just what went wrong.
function errorResult(message: string) {
	return { isError: true, content: [{ type: "text" as const, text: message }] };
}

const fleetArg = z
	.string()
	.min(1)
	.describe("Fleet name, as reported in the `deployment.fleet` field of bobi_fleet_status.");

const instanceArg = z
	.string()
	.min(1)
	.describe("Instance name, as reported in the `deployment.instance` field of bobi_fleet_status.");

/**
 * Build the fleet MCP server for one request.
 *
 * `store` is injected rather than derived from `env` so the tool bodies are
 * testable against the same in-memory `FleetStorage` the fleet suite already
 * uses, without a KV binding.
 */
export function createFleetMcpServer(store: FleetStorage, env: McpEnv): McpServer {
	const server = new McpServer(
		{ name: MCP_SERVER_NAME, version: MCP_SERVER_VERSION },
		{ instructions: SERVER_INSTRUCTIONS },
	);

	const windows = () => windowsFromEnv(env.FLEET_LIVE_WINDOW_S, env.FLEET_STALE_WINDOW_S);

	server.registerTool(
		"bobi_fleet_status",
		{
			title: "Fleet status",
			description:
				"The orienting read: every instance in the fleet with its reachability, " +
				"manager state, running sessions, and deployed versions. Takes no arguments. " +
				"Call this first - the (fleet, instance) pairs every other tool needs come " +
				"from here. An instance that has never sent a heartbeat is not listed.",
			annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
		},
		async () => jsonResult(await buildFleetStatus(store, Date.now(), windows())),
	);

	server.registerTool(
		"bobi_instance_detail",
		{
			title: "Instance detail",
			description:
				"Everything the fleet knows about one instance: its full last heartbeat " +
				"(manager, sessions, versions, spend) plus the lifecycle trail - the " +
				"start/stop/restart events its supervisor reported, newest last. Use this " +
				"to see why an instance looks wrong in bobi_fleet_status.",
			inputSchema: z.object({ fleet: fleetArg, instance: instanceArg }),
			annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
		},
		async ({ fleet, instance }) => {
			const detail = await buildInstanceDetail(store, fleet, instance, Date.now(), windows());
			if (!detail) {
				return errorResult(
					`No instance ${fleet}/${instance} in the fleet read model. ` +
						"Check the exact names with bobi_fleet_status - an instance appears only " +
						"once its supervisor has sent a heartbeat.",
				);
			}
			return jsonResult(detail);
		},
	);

	server.registerTool(
		"bobi_command_result",
		{
			title: "Command result",
			description:
				"Read back an admin command by id. `status` is \"pending\" until the target " +
				"instance's supervisor replies, then \"done\" or \"error\". Commands are issued " +
				"by the tools arriving in a later phase and by the hosted console; this tool " +
				"reads the result of any of them, so it is also the trail showing what was run " +
				"against an instance.",
			inputSchema: z.object({
				fleet: fleetArg,
				instance: instanceArg,
				command_id: z.string().min(1).describe("The command id returned when the command was issued."),
			}),
			annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
		},
		async ({ fleet, instance, command_id }) => {
			const view = await buildCommandView(store, fleet, instance, command_id);
			if (!view) {
				return errorResult(
					`No command ${command_id} on ${fleet}/${instance}. Command ids are scoped to ` +
						"the instance they were issued against, so check the fleet and instance too.",
				);
			}
			return jsonResult(view);
		},
	);

	return server;
}

/**
 * Serve one MCP request.
 *
 * The caller authenticates first - `index.ts` runs `requireOperator` before
 * dispatching here, exactly as it does for `/fleet/*`, so an unauthenticated
 * request is rejected before any tool body runs.
 *
 * CORS is off. Browser-based fleet control is a non-goal (the credential is a
 * bearer token held by an operator's client, not something a page should be
 * able to spend), so the handler advertises no cross-origin access at all.
 */
export function handleMcpRequest(request: Request, env: McpEnv, ctx?: ExecutionContext): Promise<Response> {
	const store = createFleetKVStorage(env.EVENTS);
	const handler = createMcpHandler(() => createFleetMcpServer(store, env), {
		route: MCP_ROUTE,
		corsOptions: false,
	});
	// `ctx` is optional on the Worker's own fetch signature (the suites call it
	// without one); the handler only uses it for waitUntil.
	return handler(request, env, ctx ?? ({ waitUntil: () => {}, passThroughOnException: () => {} } as unknown as ExecutionContext));
}
