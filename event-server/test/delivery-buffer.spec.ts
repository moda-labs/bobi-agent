import { describe, it, expect } from "vitest";
import type { NormalizedEvent } from "@moda-labs/bobi-events-core";
import {
	pushAndSend,
	MAX_BUFFER,
	type BufferedDeployment,
	type DeliverySocket,
} from "../src/delivery-buffer";

// D090: the three copies of this block in storage.deliver() had to stay in
// lockstep by hand. These are the invariants all three shared.

function makeEvent(text = "hello"): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "slack",
		type: "slack.mention",
		topics: ["slack:T123"],
		delivery: "chat",
		text,
		timestamp: new Date().toISOString(),
		payload: {},
	};
}

class FakeSocket implements DeliverySocket {
	sent: string[] = [];
	constructor(private readonly throwOnSend = false) {}
	send(data: string): void {
		if (this.throwOnSend) throw new Error("socket is gone");
		this.sent.push(data);
	}
}

function makeDeployment(...sockets: FakeSocket[]): BufferedDeployment<FakeSocket> {
	return { nextSeq: 1, eventBuffer: [], websockets: new Set(sockets) };
}

describe("pushAndSend", () => {
	it("assigns sequence numbers monotonically from nextSeq", () => {
		const dep = makeDeployment();
		expect(pushAndSend(dep, makeEvent()).seq).toBe(1);
		expect(pushAndSend(dep, makeEvent()).seq).toBe(2);
		expect(dep.nextSeq).toBe(3);
	});

	it("appends the sequenced event to the replay buffer", () => {
		const dep = makeDeployment();
		const event = makeEvent("buffered");
		pushAndSend(dep, event);
		expect(dep.eventBuffer).toHaveLength(1);
		expect(dep.eventBuffer[0]!.text).toBe("buffered");
		expect(dep.eventBuffer[0]!.seq).toBe(1);
	});

	it("broadcasts one JSON event frame to every live socket", () => {
		const a = new FakeSocket();
		const b = new FakeSocket();
		const dep = makeDeployment(a, b);
		const seqEvent = pushAndSend(dep, makeEvent("fanout"));
		const expected = JSON.stringify({ type: "event", data: seqEvent });
		expect(a.sent).toEqual([expected]);
		expect(b.sent).toEqual([expected]);
	});

	it("drops a socket whose send throws, without disturbing the others", () => {
		const dead = new FakeSocket(true);
		const live = new FakeSocket();
		const dep = makeDeployment(dead, live);
		pushAndSend(dep, makeEvent());
		expect(dep.websockets.has(dead)).toBe(false);
		expect(dep.websockets.has(live)).toBe(true);
		expect(live.sent).toHaveLength(1);
	});

	it("trims the buffer back to maxBuffer once it reaches twice that", () => {
		const dep = makeDeployment();
		for (let i = 0; i < 20; i++) pushAndSend(dep, makeEvent(`e${i}`), 10);
		expect(dep.eventBuffer).toHaveLength(10);
		// The trim keeps the newest events, so replay resumes at the tail.
		expect(dep.eventBuffer[dep.eventBuffer.length - 1]!.text).toBe("e19");
		// ...and sequence numbers keep counting past the trim.
		expect(dep.nextSeq).toBe(21);
	});

	it("defaults to the shared MAX_BUFFER when no limit is passed", () => {
		const dep = makeDeployment();
		pushAndSend(dep, makeEvent());
		expect(MAX_BUFFER).toBe(10_000);
		expect(dep.eventBuffer).toHaveLength(1);
	});
});
