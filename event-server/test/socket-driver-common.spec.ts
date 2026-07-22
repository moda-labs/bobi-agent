import { describe, expect, it, vi } from "vitest";
import {
	calculateBackoffDelay,
	clearScheduledTimeout,
	disposeWebSocket,
	isCurrentGeneration,
	scheduleUnrefTimeout,
} from "../src/socket-driver-common";

describe("socket driver common scaffolding", () => {
	it("preserves Discord jitter while supporting Slack floors and caps", () => {
		expect(calculateBackoffDelay({
			attempt: 0, baseMs: 1_000, maxMs: 60_000, random: () => 0.5,
		})).toBe(750);
		expect(calculateBackoffDelay({
			attempt: 0, baseMs: 5_000, maxMs: 60_000,
			minimumMs: 5_000, random: () => 0,
		})).toBe(5_000);
		expect(calculateBackoffDelay({
			attempt: 20, baseMs: 5_000, maxMs: 60_000,
			minimumMs: 120_000, random: () => 1,
		})).toBe(120_000);
	});

	it("creates unrefed one-shot timers and clears them safely", () => {
		const callback = vi.fn();
		const timer = scheduleUnrefTimeout(callback, 60_000);
		expect(timer.hasRef()).toBe(false);
		clearScheduledTimeout(timer);
		expect(callback).not.toHaveBeenCalled();
		expect(clearScheduledTimeout(null)).toBeNull();
	});

	it("guards callbacks by the owning connection generation", () => {
		const owner = { generation: 4 };
		expect(isCurrentGeneration(owner, 4)).toBe(true);
		expect(isCurrentGeneration(owner, 3)).toBe(false);
	});

	it("disposes a socket with a retained error sink", () => {
		const calls: string[] = [];
		const socket = {
			removeAllListeners: () => { calls.push("remove"); },
			on: (event: string) => { calls.push(`on:${event}`); },
			close: (code: number, reason: string) => { calls.push(`close:${code}:${reason}`); },
			terminate: () => { calls.push("terminate"); },
		};

		disposeWebSocket(socket, 4000, "reconnecting");
		expect(calls).toEqual([
			"remove",
			"on:error",
			"close:4000:reconnecting",
		]);
	});

	it("terminates when graceful disposal throws", () => {
		const terminate = vi.fn();
		const socket = {
			removeAllListeners: vi.fn(),
			on: vi.fn(),
			close: () => { throw new Error("still connecting"); },
			terminate,
		};

		disposeWebSocket(socket, 4000, "reconnecting");
		expect(terminate).toHaveBeenCalledOnce();
	});
});
