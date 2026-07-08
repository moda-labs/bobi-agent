/**
 * Delivery-path circuit breaker (#299, split from #215 loop-safety).
 *
 * Keying decision: the breaker keys on `(deployment_id, conversation)` where
 * conversation is derived from external channel identity:
 *   - Slack: thread_ts (or ts if no thread)
 *   - GitHub: repo + issue/PR number
 *   - Other: event type (coarse — acceptable because custom events rarely loop)
 *
 * EXEMPTIONS: `inbox/*` and `reply/*` topics are legitimate internal
 * agent↔agent comms and are NEVER counted toward the breaker. They flow
 * through a separate routing namespace (`agent/*` topics) that does not
 * represent external conversation depth.
 *
 * Trip condition: ≥ THRESHOLD non-human-authored deliveries in one key within
 * WINDOW_MS with zero human events → pause delivery for that key (buffered,
 * not dropped). Emit `system.loop_detected`. Auto-resume after COOLDOWN_MS or
 * on the next human-authored event in the same key.
 */

import type { NormalizedEvent } from "./core.js";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

export const BREAKER_THRESHOLD = 5;
export const BREAKER_WINDOW_MS = 60_000; // 60s
export const BREAKER_COOLDOWN_MS = 300_000; // 5min

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface BreakerState {
	/** Timestamps of non-human deliveries within the current window */
	timestamps: number[];
	/** Whether the breaker is currently tripped */
	tripped: boolean;
	/** When the breaker was tripped (epoch ms), undefined if not tripped */
	trippedAt?: number;
	/** Events paused while tripped — delivered on resume */
	paused: NormalizedEvent[];
}

export interface BreakerVerdict {
	/** Whether delivery should proceed */
	allow: boolean;
	/** Whether this delivery tripped the breaker (first trip) */
	justTripped: boolean;
}

const AGENT_LIFECYCLE_EVENTS = new Set([
	"agent/session.completed",
	"agent/session.failed",
	"session.completed",
	"session.failed",
]);

// ---------------------------------------------------------------------------
// Conversation key extraction
// ---------------------------------------------------------------------------

/**
 * Returns true if this event's type is exempt from breaker counting.
 * inbox/* and reply/* are internal agent↔agent comms, not external
 * conversation depth.
 */
export function isExemptFromBreaker(event: NormalizedEvent): boolean {
	const t = event.type;
	if (t.startsWith("inbox/") || t.startsWith("reply/")) return true;
	if (event.source === "agent" && AGENT_LIFECYCLE_EVENTS.has(t)) return true;
	// Also check topics — events published on inbox/* or reply/* topics
	// (via createTopicEvent) carry those as their routing keys.
	if (event.topics?.some((topic) => topic.startsWith("inbox/") || topic.startsWith("reply/"))) {
		return true;
	}
	if (event.source === "agent" && event.topics?.some((topic) => AGENT_LIFECYCLE_EVENTS.has(topic))) {
		return true;
	}
	return false;
}

/**
 * Extract the conversation key from an event. Returns null if no meaningful
 * conversation can be identified (breaker won't apply).
 */
export function conversationKey(event: NormalizedEvent): string | null {
	const payload = event.payload as Record<string, unknown> | undefined;
	const fields = event.fields as Record<string, unknown> | undefined;
	const payloadString = (key: string): string => {
		const value = payload?.[key];
		return typeof value === "string" ? value.trim() : "";
	};

	if (event.source === "slack") {
		// Slack: thread_ts identifies a conversation (thread). If no thread,
		// use ts (the message itself becomes the "thread" root).
		const threadTs = (payload?.thread_ts as string) || (fields?.thread_ts as string) || "";
		const ts = (payload?.ts as string) || (fields?.ts as string) || "";
		const channel = (payload?.channel as string) || (fields?.channel as string) || "";
		const key = threadTs || ts;
		if (key && channel) return `slack:${channel}:${key}`;
		if (key) return `slack:${key}`;
		return null;
	}

	if (event.source === "github") {
		// GitHub: repo + issue/PR number identifies the conversation.
		const repo = (payload?.repository as Record<string, unknown>)?.full_name as string | undefined;
		const issue = payload?.issue as Record<string, unknown> | undefined;
		const pr = payload?.pull_request as Record<string, unknown> | undefined;
		const num = (issue?.number ?? pr?.number) as number | undefined;
		if (repo && num !== undefined) return `github:${repo}#${num}`;
		// Fall back to repo-level for events without an issue/PR (e.g. push)
		if (repo) return `github:${repo}`;
		return null;
	}

	if (event.source === "monitor") {
		// Monitor findings are often batched by one scheduled run but emitted as
		// separate actionable findings. Key by the monitor plus the scheduler's
		// stable finding identity so one audit pass with several distinct
		// findings does not look like a self-reinforcing event loop.
		const monitor = payloadString("monitor");
		const findingKey = payloadString("finding_key") || payloadString("key");
		if (monitor && findingKey) {
			return `monitor:${encodeURIComponent(monitor)}:${encodeURIComponent(findingKey)}`;
		}
		if (monitor) return `monitor:${encodeURIComponent(monitor)}`;
		return `monitor:${encodeURIComponent(event.type)}`;
	}

	// Fallback for custom/linear events: use the event type as a coarse key.
	// This means N different events of the same type within one deployment
	// share a single bucket — acceptable because custom loops are rare and
	// the breaker trips only on sustained depth.
	return `other:${event.type}`;
}

/**
 * Determine whether an event is authored by a bot (non-human).
 * Human-authored events reset the breaker window.
 */
export function isBotAuthored(event: NormalizedEvent): boolean {
	const payload = event.payload as Record<string, unknown> | undefined;

	if (event.source === "slack") {
		// Slack: bot_id present on the inner event means a bot posted it.
		const innerEvent = payload?.event as Record<string, unknown> | undefined;
		const botId = (innerEvent?.bot_id as string) || (payload?.bot_id as string) || "";
		return !!botId;
	}

	if (event.source === "github") {
		// GitHub: sender.type === "Bot" or well-known agent logins.
		const sender = payload?.sender as Record<string, unknown> | undefined;
		if (sender?.type === "Bot") return true;
		const login = (sender?.login as string) || "";
		// Common bot suffixes: [bot], -bot, _bot
		if (login.endsWith("[bot]") || login.endsWith("-bot") || login.endsWith("_bot")) return true;
		return false;
	}

	// For custom/linear sources: conservatively treat as bot-authored if the
	// source field suggests automation. This avoids false negatives on agent-
	// published events while still allowing human-triggered custom events to
	// reset the breaker.
	const source = event.source || "";
	if (source === "agent" || source === "monitor" || source === "system") return true;

	return false;
}

// ---------------------------------------------------------------------------
// Breaker engine (in-memory, per-process)
// ---------------------------------------------------------------------------

/** Map of `deploymentId:conversationKey` → BreakerState */
const states = new Map<string, BreakerState>();

function getState(compositeKey: string): BreakerState {
	let s = states.get(compositeKey);
	if (!s) {
		s = { timestamps: [], tripped: false, paused: [] };
		states.set(compositeKey, s);
	}
	return s;
}

/**
 * Prune timestamps outside the current window.
 */
function pruneWindow(state: BreakerState, now: number): void {
	const cutoff = now - BREAKER_WINDOW_MS;
	state.timestamps = state.timestamps.filter((t) => t > cutoff);
}

/**
 * Check whether a tripped breaker has cooled down and should auto-resume.
 */
function checkCooldown(state: BreakerState, now: number): boolean {
	if (!state.tripped || !state.trippedAt) return false;
	return now - state.trippedAt >= BREAKER_COOLDOWN_MS;
}

/**
 * Record a delivery attempt and return a verdict.
 *
 * Call this BEFORE actually delivering the event. If `allow` is false,
 * the caller must buffer the event (it is already added to state.paused).
 */
export function recordDelivery(
	deploymentId: string,
	event: NormalizedEvent,
): BreakerVerdict {
	const convKey = conversationKey(event);
	if (!convKey) return { allow: true, justTripped: false };

	const compositeKey = `${deploymentId}:${convKey}`;
	const state = getState(compositeKey);
	const now = Date.now();

	// Auto-resume on cooldown expiry
	if (state.tripped && checkCooldown(state, now)) {
		resumeState(state);
	}

	// Human event resets the breaker
	if (!isBotAuthored(event)) {
		resumeState(state);
		return { allow: true, justTripped: false };
	}

	// If already tripped, buffer the event
	if (state.tripped) {
		state.paused.push(event);
		return { allow: false, justTripped: false };
	}

	// Record this bot delivery
	pruneWindow(state, now);
	state.timestamps.push(now);

	// Check threshold
	if (state.timestamps.length >= BREAKER_THRESHOLD) {
		state.tripped = true;
		state.trippedAt = now;
		state.paused.push(event);
		return { allow: false, justTripped: true };
	}

	return { allow: true, justTripped: false };
}

/**
 * Resume a tripped breaker — returns any paused events that should now
 * be delivered.
 */
function resumeState(state: BreakerState): void {
	state.tripped = false;
	state.trippedAt = undefined;
	state.timestamps = [];
	// Note: paused events are drained by the caller via drainPaused()
}

/**
 * Drain paused events for a deployment+conversation. Returns the events
 * that were buffered and clears the pause buffer. The caller is responsible
 * for delivering them.
 */
export function drainPaused(deploymentId: string, event: NormalizedEvent): NormalizedEvent[] {
	const convKey = conversationKey(event);
	if (!convKey) return [];
	const compositeKey = `${deploymentId}:${convKey}`;
	const state = states.get(compositeKey);
	if (!state || state.paused.length === 0) return [];
	const drained = state.paused.splice(0);
	return drained;
}

/**
 * Check whether a breaker is currently tripped for a deployment+conversation.
 * Used after cooldown to decide whether to flush paused events.
 */
export function isBreakerTripped(deploymentId: string, convKey: string): boolean {
	const compositeKey = `${deploymentId}:${convKey}`;
	const state = states.get(compositeKey);
	if (!state) return false;
	if (state.tripped && state.trippedAt && checkCooldown(state, Date.now())) {
		resumeState(state);
		return false;
	}
	return state.tripped;
}

/**
 * Build the system.loop_detected event to be published.
 */
export function buildLoopDetectedEvent(
	deploymentId: string,
	convKey: string,
	triggerEvent: NormalizedEvent,
): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "system",
		type: "system.loop_detected",
		topics: ["system.loop_detected"],
		delivery: "bulk",
		text: `Circuit breaker tripped: deployment=${deploymentId} conversation=${convKey} trigger=${triggerEvent.type}`,
		fields: {
			deployment_id: deploymentId,
			conversation_key: convKey,
			trigger_event_type: triggerEvent.type,
			trigger_event_id: triggerEvent.id,
			threshold: BREAKER_THRESHOLD,
			window_ms: BREAKER_WINDOW_MS,
			cooldown_ms: BREAKER_COOLDOWN_MS,
		},
		timestamp: new Date().toISOString(),
		payload: {
			deployment_id: deploymentId,
			conversation_key: convKey,
			trigger_event: triggerEvent,
		},
	};
}

// ---------------------------------------------------------------------------
// Reset (for testing)
// ---------------------------------------------------------------------------

export function resetAllBreakers(): void {
	states.clear();
}
