import { SELF } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import { mintBubble, publishSigned, registerSigned } from "./bubble-helpers";
import {
	type FleetCommandRecord,
	type FleetCommandResultRecord,
	type FleetInstanceRecord,
	type FleetLifecycleRecord,
	type FleetSnapshot,
	type FleetStorage,
	adminTopic,
	buildCommandView,
	buildFleetStatus,
	buildInstanceDetail,
	foldCommandResult,
	foldHeartbeat,
	foldLifecycle,
	isAdminCommand,
	reachability,
} from "../src/fleet";

// ---------------------------------------------------------------------------
// In-memory FleetStorage - the adapter seam makes the fold/query pure-testable
// without the Worker (the same discipline core uses for StorageAdapter).
// ---------------------------------------------------------------------------

function memStore(): FleetStorage {
	const instances = new Map<string, FleetInstanceRecord>();
	const trails = new Map<string, FleetLifecycleRecord[]>();
	const commands = new Map<string, FleetCommandRecord>();
	const results = new Map<string, FleetCommandResultRecord>();
	const key = (f: string, i: string) => `${f} ${i}`;
	const ckey = (f: string, i: string, id: string) => `${f} ${i} ${id}`;
	return {
		async putInstance(r) {
			instances.set(key(r.snapshot.deployment.fleet, r.snapshot.deployment.instance), r);
		},
		async getInstance(f, i) {
			return instances.get(key(f, i)) ?? null;
		},
		async listInstances() {
			return [...instances.values()];
		},
		async appendLifecycle(f, i, r) {
			const k = key(f, i);
			const arr = trails.get(k) ?? [];
			arr.push(r);
			trails.set(k, arr);
		},
		async listLifecycle(f, i) {
			return [...(trails.get(key(f, i)) ?? [])].sort((a, b) => b.received_at - a.received_at);
		},
		async putCommand(r) {
			commands.set(ckey(r.fleet, r.instance, r.command_id), r);
		},
		async getCommand(f, i, id) {
			return commands.get(ckey(f, i, id)) ?? null;
		},
		async putCommandResult(f, i, r) {
			results.set(ckey(f, i, r.command_id), r);
		},
		async getCommandResult(f, i, id) {
			return results.get(ckey(f, i, id)) ?? null;
		},
	};
}

function snapshot(fleet: string, instance: string, over: Partial<FleetSnapshot> = {}): FleetSnapshot {
	return {
		deployment: { fleet, instance, platform: "fly", machine: "m1", region: "iad", node: null },
		supervisor: { pid: 1, uptime_s: 5, version: "0.1.0" },
		manager: { status: "idle", pid: 4242, healthy: true, idle_seconds: 3, restart_count: 0 },
		sessions: [{ name: "mgr", role: "manager", status: "idle" }],
		versions: { image: null, team_package: null, bobi: "0.41.0" },
		generated_at: "2026-07-09T00:00:00+00:00",
		...over,
	};
}

// ---------------------------------------------------------------------------
// Pure fold + query
// ---------------------------------------------------------------------------

describe("fleet fold + query", () => {
	it("folds a heartbeat latest-value and lists it live", async () => {
		const store = memStore();
		const now = 1_000_000;
		expect(await foldHeartbeat(store, snapshot("f", "i"), now)).toBe(true);

		const status = await buildFleetStatus(store, now + 1000, { liveMs: 90_000, staleMs: 300_000 });
		expect(status.instances).toHaveLength(1);
		expect(status.instances[0].deployment.instance).toBe("i");
		expect(status.instances[0].reachability).toBe("live");
		expect((status.instances[0].manager as Record<string, unknown>).status).toBe("idle");
	});

	it("overwrites in place (no growth) on the next heartbeat", async () => {
		const store = memStore();
		await foldHeartbeat(store, snapshot("f", "i", { manager: { status: "idle" } }), 1);
		await foldHeartbeat(store, snapshot("f", "i", { manager: { status: "wedged" } }), 2);
		const all = await store.listInstances();
		expect(all).toHaveLength(1); // latest-value, not append
		expect((all[0].snapshot.manager as Record<string, unknown>).status).toBe("wedged");
	});

	it("rejects a heartbeat with no (fleet,instance) key", async () => {
		const store = memStore();
		expect(await foldHeartbeat(store, { deployment: { fleet: "", instance: "" } } as FleetSnapshot, 1)).toBe(false);
		expect(await store.listInstances()).toHaveLength(0);
	});

	it("appends lifecycle into the trail (newest first) without touching the heartbeat record", async () => {
		const store = memStore();
		await foldHeartbeat(store, snapshot("f", "i"), 100);
		await foldLifecycle(store, { deployment: { fleet: "f", instance: "i" }, event: "probe_failing", status: "wedged" }, 200);
		await foldLifecycle(store, { deployment: { fleet: "f", instance: "i" }, event: "manager_restarted", reason: "wedge" }, 300);

		const detail = await buildInstanceDetail(store, "f", "i", 400, { liveMs: 90_000, staleMs: 300_000 });
		expect(detail).not.toBeNull();
		const lifecycle = (detail as Record<string, unknown>).lifecycle as Record<string, unknown>[];
		expect(lifecycle.map((e) => e.event)).toEqual(["manager_restarted", "probe_failing"]); // newest first

		// The lifecycle fold must NOT rewrite the instance record - its
		// received_at stays the heartbeat's (no read-modify-write clobber that
		// would corrupt reachability).
		const rec = await store.getInstance("f", "i");
		expect(rec?.received_at).toBe(100);
	});

	it("a lifecycle fold does not clobber a later heartbeat's received_at", async () => {
		// Interleave: heartbeat@t100 -> lifecycle@t200 -> heartbeat@t300. The
		// final record must reflect the newest heartbeat, not a stale re-put.
		const store = memStore();
		await foldHeartbeat(store, snapshot("f", "i"), 100);
		await foldLifecycle(store, { deployment: { fleet: "f", instance: "i" }, event: "probe_failing" }, 200);
		await foldHeartbeat(store, snapshot("f", "i"), 300);
		const rec = await store.getInstance("f", "i");
		expect(rec?.received_at).toBe(300);
	});

	it("derives reachability from heartbeat age", () => {
		const w = { liveMs: 90_000, staleMs: 300_000 };
		expect(reachability(1000, 1000 + 1000, w)).toBe("live");
		expect(reachability(1000, 1000 + 120_000, w)).toBe("stale");
		expect(reachability(1000, 1000 + 400_000, w)).toBe("unreachable");
	});

	it("instance detail 404s (null) for an unknown instance", async () => {
		const store = memStore();
		expect(await buildInstanceDetail(store, "f", "nope", 1, { liveMs: 1, staleMs: 2 })).toBeNull();
	});

	it("captures the authenticated bubble on the instance record", async () => {
		const store = memStore();
		await foldHeartbeat(store, snapshot("f", "i"), 1, "bubble-xyz");
		const rec = await store.getInstance("f", "i");
		expect(rec?.bubble_id).toBe("bubble-xyz");
	});
});

// ---------------------------------------------------------------------------
// Admin command fold + view (#9)
// ---------------------------------------------------------------------------

describe("admin command view", () => {
	it("gates the command vocabulary", () => {
		expect(isAdminCommand("restart")).toBe(true);
		expect(isAdminCommand("stop")).toBe(true);
		expect(isAdminCommand("start")).toBe(true);
		expect(isAdminCommand("status")).toBe(true);
		// Phase C (#11): tier-3 reads + data-plane chat ride the same vocab.
		expect(isAdminCommand("chat")).toBe(true);
		expect(isAdminCommand("transcript")).toBe(true);
		expect(isAdminCommand("roster")).toBe(true);
		// Observability (#733): the spend + session-log reads ride the same vocab.
		expect(isAdminCommand("spend")).toBe(true);
		expect(isAdminCommand("session_log")).toBe(true);
		expect(isAdminCommand("rm -rf")).toBe(false);
		expect(isAdminCommand(42)).toBe(false);
	});

	it("builds the admin topic non-globally (bubble-namespaced)", () => {
		expect(adminTopic("acme", "prod-1")).toBe("fleet/admin/acme/prod-1");
	});

	it("returns null for a never-issued command id", async () => {
		const store = memStore();
		expect(await buildCommandView(store, "f", "i", "nope")).toBeNull();
	});

	it("reads pending until the result folds, then done", async () => {
		const store = memStore();
		await store.putCommand({ command_id: "c1", fleet: "f", instance: "i", command: "restart", issued_at: 100 });

		let view = await buildCommandView(store, "f", "i", "c1");
		expect(view?.status).toBe("pending");
		expect(view?.command).toBe("restart");
		expect(view?.completed_at).toBeNull();

		expect(
			await foldCommandResult(
				store,
				{ deployment: { fleet: "f", instance: "i" }, command_id: "c1", status: "done", result: { accepted: true } },
				200,
			),
		).toBe(true);

		view = await buildCommandView(store, "f", "i", "c1");
		expect(view?.status).toBe("done");
		expect(view?.result).toEqual({ accepted: true });
		expect(view?.completed_at).toBe(200);
	});

	it("folds an error result", async () => {
		const store = memStore();
		await store.putCommand({ command_id: "c2", fleet: "f", instance: "i", command: "status", issued_at: 1 });
		await foldCommandResult(
			store,
			{ deployment: { fleet: "f", instance: "i" }, command_id: "c2", status: "error", error: "boom" },
			2,
		);
		const view = await buildCommandView(store, "f", "i", "c2");
		expect(view?.status).toBe("error");
		expect(view?.error).toBe("boom");
	});

	it("round-trips a bulk tier-3 result (transcript messages) as the command result", async () => {
		const store = memStore();
		await store.putCommand({
			command_id: "t1",
			fleet: "f",
			instance: "i",
			command: "transcript",
			args: { session: "bobi-f-manager" },
			issued_at: 10,
		});
		const messages = [
			{ role: "user", text: "ping" },
			{ role: "agent", text: "stub ack: ping" },
		];
		await foldCommandResult(
			store,
			{ deployment: { fleet: "f", instance: "i" }, command_id: "t1", status: "done", result: { messages } },
			20,
		);
		const view = await buildCommandView(store, "f", "i", "t1");
		expect(view?.status).toBe("done");
		expect(view?.command).toBe("transcript");
		expect(view?.args).toEqual({ session: "bobi-f-manager" });
		expect((view?.result as Record<string, unknown>).messages).toEqual(messages);
	});

	it("rejects a malformed command result (no id / bad status) so it can't mask a command as done", async () => {
		const store = memStore();
		expect(
			await foldCommandResult(store, { deployment: { fleet: "f", instance: "i" }, command_id: "", status: "done" }, 1),
		).toBe(false);
		expect(
			await foldCommandResult(
				store,
				{ deployment: { fleet: "f", instance: "i" }, command_id: "c", status: "weird" as unknown as "done" },
				1,
			),
		).toBe(false);
	});
});

// ---------------------------------------------------------------------------
// Through the real Worker: operator auth + signed-publish -> fold -> read
// ---------------------------------------------------------------------------

const OPERATOR = { authorization: "Bearer test-operator-token" };

describe("fleet HTTP API", () => {
	it("requires the operator credential", async () => {
		const noauth = await SELF.fetch("https://example.com/fleet/status");
		expect(noauth.status).toBe(401);
		const wrong = await SELF.fetch("https://example.com/fleet/status", {
			headers: { authorization: "Bearer nope" },
		});
		expect(wrong.status).toBe(401);
	});

	it("does not fold an unsigned publish", async () => {
		// Unsigned POST to the fleet topic must be rejected and never folded.
		const res = await SELF.fetch("https://example.com/events/fleet/heartbeat", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ source: "fleet", payload: snapshot("unsigned-f", "unsigned-i") }),
		});
		expect(res.status).toBeGreaterThanOrEqual(400);
		const detail = await SELF.fetch(
			"https://example.com/fleet/instances/unsigned-f/unsigned-i",
			{ headers: OPERATOR },
		);
		expect(detail.status).toBe(404);
	});

	it("folds a signed heartbeat and serves it via /fleet/status + instance detail", async () => {
		const b = await mintBubble();
		const fleet = "acme";
		const instance = `prod-${Date.now()}`;
		const pub = await publishSigned("fleet/heartbeat", b,
			snapshot(fleet, instance, { manager: { status: "idle", healthy: true, restart_count: 0 } }));
		expect(pub.status).toBeLessThan(300);

		const statusRes = await SELF.fetch("https://example.com/fleet/status", { headers: OPERATOR });
		expect(statusRes.status).toBe(200);
		const status = (await statusRes.json()) as { instances: { deployment: { instance: string }; reachability: string }[] };
		const mine = status.instances.find((e) => e.deployment.instance === instance);
		expect(mine).toBeTruthy();
		expect(mine!.reachability).toBe("live");

		// Publish a wedge lifecycle event; instance detail carries the trail.
		const life = await publishSigned("fleet/lifecycle", b, {
			deployment: { fleet, instance },
			event: "probe_failing",
			status: "wedged",
		});
		expect(life.status).toBeLessThan(300);

		const detailRes = await SELF.fetch(
			`https://example.com/fleet/instances/${fleet}/${instance}`,
			{ headers: OPERATOR },
		);
		expect(detailRes.status).toBe(200);
		const detail = (await detailRes.json()) as { lifecycle: { event: string }[]; deployment: { instance: string } };
		expect(detail.deployment.instance).toBe(instance);
		expect(detail.lifecycle.map((e) => e.event)).toContain("probe_failing");
	});

	it("returns 400 (not 500) for a malformed percent-encoded instance path", async () => {
		const res = await SELF.fetch("https://example.com/fleet/instances/%/x", {
			headers: OPERATOR,
		});
		expect(res.status).toBe(400);
	});

	it("commands require the operator credential", async () => {
		const post = await SELF.fetch("https://example.com/fleet/instances/f/i/commands", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ command: "restart" }),
		});
		expect(post.status).toBe(401);
	});

	it("rejects an unknown command (400)", async () => {
		const res = await SELF.fetch("https://example.com/fleet/instances/f/i/commands", {
			method: "POST",
			headers: { ...OPERATOR, "content-type": "application/json" },
			body: JSON.stringify({ command: "rm-rf" }),
		});
		expect(res.status).toBe(400);
	});

	it("404s a command to an instance that never heartbeated (no bubble to address)", async () => {
		const res = await SELF.fetch("https://example.com/fleet/instances/ghost/ghost/commands", {
			method: "POST",
			headers: { ...OPERATOR, "content-type": "application/json" },
			body: JSON.stringify({ command: "restart" }),
		});
		expect(res.status).toBe(404);
	});

	it("503s a command when the instance heartbeats but no supervisor is subscribed", async () => {
		const b = await mintBubble();
		const fleet = "acme";
		const instance = `no-sub-${Date.now()}`;
		// Heartbeat (so the instance is addressable) but NO admin subscription.
		const hb = await publishSigned("fleet/heartbeat", b, snapshot(fleet, instance));
		expect(hb.status).toBeLessThan(300);
		const post = await SELF.fetch(`https://example.com/fleet/instances/${fleet}/${instance}/commands`, {
			method: "POST",
			headers: { ...OPERATOR, "content-type": "application/json" },
			body: JSON.stringify({ command: "restart" }),
		});
		expect(post.status).toBe(503);
	});

	it("issues a command, records it pending, and folds the supervisor's result to done", async () => {
		const b = await mintBubble();
		const fleet = "acme";
		const instance = `cmd-${Date.now()}`;
		// The sidecar's admin listener: register the admin subscription under the
		// instance bubble so the command has a live subscriber to deliver to.
		const reg = await registerSigned(`${instance}-admin`, [adminTopic(fleet, instance)], b);
		expect(reg.status).toBeLessThan(300);
		// A signed heartbeat so the server captures the deployment's bubble (the
		// command route needs it to address the admin topic).
		const hb = await publishSigned("fleet/heartbeat", b, snapshot(fleet, instance));
		expect(hb.status).toBeLessThan(300);

		const post = await SELF.fetch(`https://example.com/fleet/instances/${fleet}/${instance}/commands`, {
			method: "POST",
			headers: { ...OPERATOR, "content-type": "application/json" },
			body: JSON.stringify({ command: "restart" }),
		});
		expect(post.status).toBe(202);
		const issued = (await post.json()) as { command_id: string; status: string; status_url: string; delivered_to: number };
		expect(issued.status).toBe("pending");
		expect(issued.command_id).toBeTruthy();
		expect(typeof issued.delivered_to).toBe("number");

		// GET-by-id reads pending until the supervisor replies.
		const pending = await SELF.fetch(`https://example.com${issued.status_url}`, { headers: OPERATOR });
		expect(pending.status).toBe(200);
		expect(((await pending.json()) as { status: string }).status).toBe("pending");

		// The supervisor publishes its result (signed, same bubble); the server folds it.
		const reply = await publishSigned("fleet/command_result", b, {
			deployment: { fleet, instance },
			command_id: issued.command_id,
			status: "done",
			result: { accepted: true, action: "restart" },
		});
		expect(reply.status).toBeLessThan(300);

		const done = await SELF.fetch(`https://example.com${issued.status_url}`, { headers: OPERATOR });
		expect(done.status).toBe(200);
		const view = (await done.json()) as { status: string; result: { action: string } };
		expect(view.status).toBe("done");
		expect(view.result.action).toBe("restart");
	});

	it("404s a GET for an unknown command id", async () => {
		const res = await SELF.fetch("https://example.com/fleet/instances/f/i/commands/does-not-exist", {
			headers: OPERATOR,
		});
		expect(res.status).toBe(404);
	});
});
