// Per-deployment event sequencing, replay buffer, and WebSocket fan-out for
// the standalone Node event server.
//
// Lives beside local.ts rather than inside it because local.ts is an
// entrypoint: importing it binds a port and starts the eviction sweep, so
// nothing there can be unit-tested. Same reason http-body.ts and
// slack-socket-local.ts are siblings.
//
// D090 — storage.deliver() carried three near-identical copies of this block
// (loop-detected fan-out, drained-paused replay, and the main delivery), which
// differed only in variable prefixes. Any change to one — the trim policy, the
// dead-socket cleanup — had to be mirrored into the other two by hand or
// delivery would silently diverge between the normal, drained, and loop paths.

import type { NormalizedEvent } from "@moda-labs/bobi-events-core";

/** How many events a deployment keeps for replay before the buffer is trimmed. */
export const MAX_BUFFER = 10_000;

/** The one thing this module needs from a socket. */
export interface DeliverySocket {
	send(data: string): void;
}

/** The subset of a deployment record that carries delivery state. */
export interface BufferedDeployment<S extends DeliverySocket = DeliverySocket> {
	nextSeq: number;
	eventBuffer: Array<NormalizedEvent & { seq: number }>;
	websockets: Set<S>;
}

/**
 * Assign the next sequence number to `event`, append it to the deployment's
 * replay buffer (trimming when it grows past twice MAX_BUFFER), and broadcast
 * it to every live socket — dropping any socket whose send() throws.
 *
 * @returns the sequenced event that was buffered and sent
 */
export function pushAndSend<S extends DeliverySocket>(
	dep: BufferedDeployment<S>,
	event: NormalizedEvent,
	maxBuffer: number = MAX_BUFFER,
): NormalizedEvent & { seq: number } {
	const seqEvent = { ...event, seq: dep.nextSeq++ };
	dep.eventBuffer.push(seqEvent);
	if (dep.eventBuffer.length >= 2 * maxBuffer) {
		dep.eventBuffer = dep.eventBuffer.slice(-maxBuffer);
	}

	const msg = JSON.stringify({ type: "event", data: seqEvent });
	for (const ws of dep.websockets) {
		try {
			ws.send(msg);
		} catch {
			dep.websockets.delete(ws);
		}
	}

	return seqEvent;
}
