/** Protocol-neutral mechanics shared by local persistent-socket drivers. */

export interface BackoffOptions {
	attempt: number;
	baseMs: number;
	maxMs: number;
	minimumMs?: number;
	random?: () => number;
}

export function calculateBackoffDelay(opts: BackoffOptions): number {
	const attempt = Math.max(0, Math.floor(opts.attempt));
	const capped = Math.min(opts.maxMs, opts.baseMs * 2 ** Math.min(attempt, 30));
	const sample = Math.min(1, Math.max(0, (opts.random ?? Math.random)()));
	const jittered = capped * (0.5 + sample * 0.5);
	return Math.max(opts.minimumMs ?? 0, jittered);
}

export function scheduleUnrefTimeout(
	callback: () => void,
	delayMs: number,
): NodeJS.Timeout {
	const timer = setTimeout(callback, delayMs);
	timer.unref();
	return timer;
}

export function clearScheduledTimeout(timer: NodeJS.Timeout | null): null {
	if (timer) clearTimeout(timer);
	return null;
}

export interface GenerationOwner {
	generation: number;
}

export function isCurrentGeneration(owner: GenerationOwner, generation: number): boolean {
	return owner.generation === generation;
}

export interface DisposableWebSocket {
	removeAllListeners(): unknown;
	on(event: "error", listener: (error: Error) => void): unknown;
	close(code?: number, reason?: string): unknown;
	terminate(): unknown;
}

/**
 * Detach a socket without leaving the asynchronous error emitted by closing a
 * still-CONNECTING `ws` instance unhandled.
 */
export function disposeWebSocket(
	ws: DisposableWebSocket,
	closeCode: number,
	reason: string,
): void {
	ws.removeAllListeners();
	ws.on("error", () => { /* sink - socket is being discarded */ });
	try {
		ws.close(closeCode, reason);
	} catch {
		try { ws.terminate(); } catch { /* already gone */ }
	}
}
