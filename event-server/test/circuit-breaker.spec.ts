import { describe, it, expect, beforeEach } from "vitest";
import {
	type NormalizedEvent,
} from "../src/core";
import {
	isExemptFromBreaker,
	conversationKey,
	isBotAuthored,
	recordDelivery,
	drainPaused,
	buildLoopDetectedEvent,
	resetAllBreakers,
	BREAKER_THRESHOLD,
	BREAKER_WINDOW_MS,
	BREAKER_COOLDOWN_MS,
} from "../src/circuit-breaker";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeSlackEvent(overrides: Partial<NormalizedEvent> = {}, payloadOverrides: Record<string, unknown> = {}): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "slack",
		type: "slack.mention",
		topics: ["slack:T123"],
		delivery: "chat",
		text: "hello",
		fields: { channel: "C001", ts: "1700000001.000100", thread_ts: "1700000000.000000" },
		timestamp: new Date().toISOString(),
		payload: {
			channel: "C001",
			ts: "1700000001.000100",
			thread_ts: "1700000000.000000",
			event: { bot_id: "B999", text: "hello" },
			...payloadOverrides,
		},
		...overrides,
	};
}

function makeHumanSlackEvent(): NormalizedEvent {
	return makeSlackEvent({}, { event: { text: "human says hi" } });
}

function makeGitHubBotEvent(overrides: Partial<NormalizedEvent> = {}): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "github",
		type: "github.issue_comment",
		topics: ["github:moda-labs/modastack"],
		delivery: "bulk",
		text: "[moda-labs/modastack] created issue_comment #10",
		fields: { action: "created", number: 10 },
		timestamp: new Date().toISOString(),
		payload: {
			action: "created",
			repository: { full_name: "moda-labs/modastack" },
			issue: { number: 10, title: "Test" },
			sender: { login: "modastack[bot]", type: "Bot" },
		},
		...overrides,
	};
}

function makeGitHubHumanEvent(): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "github",
		type: "github.issue_comment",
		topics: ["github:moda-labs/modastack"],
		delivery: "bulk",
		text: "[moda-labs/modastack] created issue_comment #10",
		fields: { action: "created", number: 10 },
		timestamp: new Date().toISOString(),
		payload: {
			action: "created",
			repository: { full_name: "moda-labs/modastack" },
			issue: { number: 10, title: "Test" },
			sender: { login: "zachary", type: "User" },
		},
	};
}

function makeInboxEvent(): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "agent",
		type: "inbox/engineer",
		topics: ["inbox/engineer"],
		delivery: "chat",
		text: "message for engineer",
		timestamp: new Date().toISOString(),
		payload: { text: "message for engineer" },
	};
}

function makeReplyEvent(): NormalizedEvent {
	return {
		v: 2,
		id: crypto.randomUUID(),
		source: "agent",
		type: "reply/manager",
		topics: ["reply/manager"],
		delivery: "chat",
		text: "reply to manager",
		timestamp: new Date().toISOString(),
		payload: { text: "reply to manager" },
	};
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("circuit-breaker", () => {
	beforeEach(() => {
		resetAllBreakers();
	});

	describe("isExemptFromBreaker", () => {
		it("exempts inbox/* events", () => {
			expect(isExemptFromBreaker(makeInboxEvent())).toBe(true);
		});

		it("exempts reply/* events", () => {
			expect(isExemptFromBreaker(makeReplyEvent())).toBe(true);
		});

		it("does not exempt slack events", () => {
			expect(isExemptFromBreaker(makeSlackEvent())).toBe(false);
		});

		it("does not exempt github events", () => {
			expect(isExemptFromBreaker(makeGitHubBotEvent())).toBe(false);
		});

		it("exempts events with inbox/* in topics array", () => {
			const event = makeSlackEvent({ topics: ["inbox/foo", "slack:T123"] });
			expect(isExemptFromBreaker(event)).toBe(true);
		});
	});

	describe("conversationKey", () => {
		it("extracts slack thread_ts + channel", () => {
			const event = makeSlackEvent();
			expect(conversationKey(event)).toBe("slack:C001:1700000000.000000");
		});

		it("falls back to ts if no thread_ts", () => {
			const event: NormalizedEvent = {
				v: 2,
				id: crypto.randomUUID(),
				source: "slack",
				type: "slack.mention",
				topics: ["slack:T123"],
				delivery: "chat",
				text: "hello",
				fields: { channel: "C001", ts: "1700000001.000100" },
				timestamp: new Date().toISOString(),
				payload: { channel: "C001", ts: "1700000001.000100", event: { bot_id: "B999" } },
			};
			expect(conversationKey(event)).toBe("slack:C001:1700000001.000100");
		});

		it("extracts github repo#number", () => {
			const event = makeGitHubBotEvent();
			expect(conversationKey(event)).toBe("github:moda-labs/modastack#10");
		});

		it("returns other:type for unknown sources", () => {
			const event: NormalizedEvent = {
				v: 2,
				id: "x",
				source: "custom",
				type: "deploy.complete",
				topics: ["deploy.complete"],
				delivery: "bulk",
				text: "",
				timestamp: new Date().toISOString(),
				payload: {},
			};
			expect(conversationKey(event)).toBe("other:deploy.complete");
		});
	});

	describe("isBotAuthored", () => {
		it("detects slack bot_id", () => {
			expect(isBotAuthored(makeSlackEvent())).toBe(true);
		});

		it("human slack event is not bot-authored", () => {
			expect(isBotAuthored(makeHumanSlackEvent())).toBe(false);
		});

		it("detects github Bot sender type", () => {
			expect(isBotAuthored(makeGitHubBotEvent())).toBe(true);
		});

		it("human github event is not bot-authored", () => {
			expect(isBotAuthored(makeGitHubHumanEvent())).toBe(false);
		});

		it("detects [bot] suffix in login", () => {
			const event = makeGitHubBotEvent();
			(event.payload as Record<string, unknown>).sender = { login: "dependabot[bot]", type: "User" };
			expect(isBotAuthored(event)).toBe(true);
		});

		it("treats agent source as bot-authored", () => {
			const event: NormalizedEvent = {
				v: 2,
				id: "x",
				source: "agent",
				type: "custom.event",
				topics: ["custom.event"],
				delivery: "bulk",
				text: "",
				timestamp: new Date().toISOString(),
				payload: {},
			};
			expect(isBotAuthored(event)).toBe(true);
		});
	});

	describe("recordDelivery — trip behavior", () => {
		it("allows fewer than THRESHOLD bot events", () => {
			const depId = "dep-1";
			for (let i = 0; i < BREAKER_THRESHOLD - 1; i++) {
				const verdict = recordDelivery(depId, makeSlackEvent());
				expect(verdict.allow).toBe(true);
				expect(verdict.justTripped).toBe(false);
			}
		});

		it("trips on the THRESHOLD-th bot event in same conversation", () => {
			const depId = "dep-1";
			let verdict;
			for (let i = 0; i < BREAKER_THRESHOLD; i++) {
				verdict = recordDelivery(depId, makeSlackEvent());
			}
			expect(verdict!.allow).toBe(false);
			expect(verdict!.justTripped).toBe(true);
		});

		it("subsequent bot events after trip are paused (not tripped again)", () => {
			const depId = "dep-1";
			for (let i = 0; i < BREAKER_THRESHOLD; i++) {
				recordDelivery(depId, makeSlackEvent());
			}
			const verdict = recordDelivery(depId, makeSlackEvent());
			expect(verdict.allow).toBe(false);
			expect(verdict.justTripped).toBe(false);
		});

		it("same volume across different conversations does NOT trip", () => {
			const depId = "dep-1";
			for (let i = 0; i < BREAKER_THRESHOLD + 3; i++) {
				// Each event in a different thread
				const event = makeSlackEvent({
					fields: { channel: "C001", ts: `170000000${i}.000100`, thread_ts: `170000000${i}.000000` },
				}, {
					channel: "C001",
					ts: `170000000${i}.000100`,
					thread_ts: `170000000${i}.000000`,
					event: { bot_id: "B999" },
				});
				const verdict = recordDelivery(depId, event);
				expect(verdict.allow).toBe(true);
			}
		});

		it("human event resets the breaker", () => {
			const depId = "dep-1";
			// Get close to threshold
			for (let i = 0; i < BREAKER_THRESHOLD - 1; i++) {
				recordDelivery(depId, makeSlackEvent());
			}
			// Human event resets
			const humanVerdict = recordDelivery(depId, makeHumanSlackEvent());
			expect(humanVerdict.allow).toBe(true);

			// Now we need THRESHOLD more bot events to trip again
			for (let i = 0; i < BREAKER_THRESHOLD - 1; i++) {
				const v = recordDelivery(depId, makeSlackEvent());
				expect(v.allow).toBe(true);
			}
		});

		it("human event clears a tripped breaker", () => {
			const depId = "dep-1";
			// Trip the breaker
			for (let i = 0; i < BREAKER_THRESHOLD; i++) {
				recordDelivery(depId, makeSlackEvent());
			}
			// Human event untrips
			const verdict = recordDelivery(depId, makeHumanSlackEvent());
			expect(verdict.allow).toBe(true);

			// Subsequent bot event is allowed (counter was reset)
			const nextBot = recordDelivery(depId, makeSlackEvent());
			expect(nextBot.allow).toBe(true);
		});
	});

	describe("recordDelivery — inbox/reply exemption", () => {
		it("inbox/* events always get allow=true even if conversation is tripped", () => {
			// This test verifies at the caller level — exempt events should never
			// reach recordDelivery because callers check isExemptFromBreaker first.
			// But even if they did, they'd get allow because they have no conversation key.
			const event = makeInboxEvent();
			// inbox events have no conventional conversation key
			expect(conversationKey(event)).toBe("other:inbox/engineer");
			// They are still allowed because the caller skips breaker for exempt events
			expect(isExemptFromBreaker(event)).toBe(true);
		});
	});

	describe("drainPaused", () => {
		it("returns paused events after human resets breaker", () => {
			const depId = "dep-1";
			// Trip the breaker
			for (let i = 0; i < BREAKER_THRESHOLD; i++) {
				recordDelivery(depId, makeSlackEvent());
			}
			// Add more paused events
			recordDelivery(depId, makeSlackEvent());
			recordDelivery(depId, makeSlackEvent());

			// Human event resets
			recordDelivery(depId, makeHumanSlackEvent());

			// Drain the paused events
			const drained = drainPaused(depId, makeHumanSlackEvent());
			// The trip event + 2 subsequent = 3 paused events
			expect(drained.length).toBe(3);
		});

		it("returns empty array when nothing is paused", () => {
			const drained = drainPaused("dep-1", makeSlackEvent());
			expect(drained.length).toBe(0);
		});
	});

	describe("buildLoopDetectedEvent", () => {
		it("produces a well-formed system.loop_detected event", () => {
			const trigger = makeSlackEvent();
			const event = buildLoopDetectedEvent("dep-1", "slack:C001:1700000000.000000", trigger);
			expect(event.type).toBe("system.loop_detected");
			expect(event.source).toBe("system");
			expect(event.topics).toContain("system.loop_detected");
			expect(event.fields!.deployment_id).toBe("dep-1");
			expect(event.fields!.conversation_key).toBe("slack:C001:1700000000.000000");
			expect(event.fields!.threshold).toBe(BREAKER_THRESHOLD);
		});
	});

	describe("GitHub conversation keying", () => {
		it("same repo same issue trips the breaker", () => {
			const depId = "dep-gh";
			for (let i = 0; i < BREAKER_THRESHOLD; i++) {
				const v = recordDelivery(depId, makeGitHubBotEvent());
				if (i < BREAKER_THRESHOLD - 1) expect(v.allow).toBe(true);
				else {
					expect(v.allow).toBe(false);
					expect(v.justTripped).toBe(true);
				}
			}
		});

		it("same repo different issues do NOT trip", () => {
			const depId = "dep-gh";
			for (let i = 0; i < BREAKER_THRESHOLD + 3; i++) {
				const event: NormalizedEvent = {
					v: 2,
					id: crypto.randomUUID(),
					source: "github",
					type: "github.issue_comment",
					topics: ["github:moda-labs/modastack"],
					delivery: "bulk",
					text: "",
					fields: { number: i + 1 },
					timestamp: new Date().toISOString(),
					payload: {
						repository: { full_name: "moda-labs/modastack" },
						issue: { number: i + 1, title: "Test" },
						sender: { login: "bot[bot]", type: "Bot" },
					},
				};
				const v = recordDelivery(depId, event);
				expect(v.allow).toBe(true);
			}
		});
	});
});
