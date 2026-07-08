import { describe, it, expect } from "vitest";
import {
	buildConversation,
	parseConversation,
	slackChatType,
	slackConversation,
} from "@moda-labs/bobi-events-core/conversation";

// GOLDEN VECTOR - keep identical to tests/test_conversation.py. The grammar
// is hand-mirrored across TS and Python; this shared vector turns drift into
// a test failure (same pattern as the bubble-signing parity vector).
const GOLDEN_REF = "slack:T0GOLD:channel:C0GOLD:thread:1718000000.000042";
const GOLDEN_PARSED = {
	source: "slack",
	scope: "T0GOLD",
	chatType: "channel" as const,
	chatId: "C0GOLD",
	threadId: "1718000000.000042",
};

describe("cross-language golden vector", () => {
	it("parses and rebuilds the golden ref", () => {
		expect(parseConversation(GOLDEN_REF)).toEqual(GOLDEN_PARSED);
		expect(buildConversation(GOLDEN_PARSED)).toBe(GOLDEN_REF);
	});
});

describe("buildConversation / parseConversation", () => {
	it("round-trips a ref without a thread", () => {
		const conv = {
			source: "slack",
			scope: "T0123",
			chatType: "dm" as const,
			chatId: "D0789",
		};
		const ref = buildConversation(conv);
		expect(ref).toBe("slack:T0123:dm:D0789");
		expect(parseConversation(ref)).toEqual(conv);
	});

	it("round-trips a ref with a thread", () => {
		const conv = {
			source: "slack",
			scope: "T0123",
			chatType: "channel" as const,
			chatId: "C0456",
			threadId: "1718000000.123456",
		};
		const ref = buildConversation(conv);
		expect(ref).toBe("slack:T0123:channel:C0456:thread:1718000000.123456");
		expect(parseConversation(ref)).toEqual(conv);
	});

	it("parses a whatsapp-shaped ref", () => {
		expect(parseConversation("whatsapp:747556541:dm:15551234567")).toEqual({
			source: "whatsapp",
			scope: "747556541",
			chatType: "dm",
			chatId: "15551234567",
		});
	});

	it.each([
		["", "empty"],
		["slack:T0123:dm", "too few segments"],
		["slack:T0123:dm:D0789:extra", "five segments"],
		["slack:T0123:dm:D0789:thread:1.2:extra", "too many segments"],
		["slack:T0123:dm:D0789:topic:1.2", "unknown trailer keyword"],
		["slack:T0123:channel:", "empty chat id"],
		["slack::channel:C1", "empty scope"],
		["slack:T0123:mpim:C1", "unknown chat type"],
	])("rejects %s (%s)", (ref) => {
		expect(parseConversation(ref)).toBeNull();
	});

	it("returns null for a non-string ref instead of throwing", () => {
		expect(parseConversation(["slack", "T1"] as unknown as string)).toBeNull();
		expect(parseConversation(42 as unknown as string)).toBeNull();
	});

	// The grammar's core invariant is enforced at build time: a colon-bearing
	// id must fail loudly, never emit a ref that mis-parses later.
	it("throws on segment values containing the delimiter", () => {
		expect(() => buildConversation({
			source: "slack", scope: "T1", chatType: "channel", chatId: "a:thread:b",
		})).toThrow(/invalid conversation segment/);
		expect(() => buildConversation({
			source: "slack", scope: "T1", chatType: "channel", chatId: "C1", threadId: "1:2",
		})).toThrow(/invalid conversation segment/);
		expect(() => buildConversation({
			source: "slack", scope: "", chatType: "dm", chatId: "D1",
		})).toThrow(/invalid conversation segment/);
	});
});

describe("slackChatType", () => {
	it.each([
		["im", "dm"],
		["mpim", "group"],
		["channel", "channel"],
		["group", "channel"], // Slack "group" = private channel
		["", "channel"],
	])("maps %s to %s", (channelType, expected) => {
		expect(slackChatType(channelType)).toBe(expected);
	});
});

describe("slackConversation", () => {
	it("anchors on thread_ts when present", () => {
		expect(slackConversation("T1", "C1", "channel", "12.99", "12.34"))
			.toBe("slack:T1:channel:C1:thread:12.34");
	});

	it("anchors a top-level message on its own ts", () => {
		expect(slackConversation("T1", "C1", "channel", "12.99"))
			.toBe("slack:T1:channel:C1:thread:12.99");
	});

	it("builds a DM ref without a thread when no ts is available", () => {
		expect(slackConversation("T1", "D1", "im", ""))
			.toBe("slack:T1:dm:D1");
	});

	it("returns undefined for missing ids or delimiter-bearing ids", () => {
		expect(slackConversation("", "C1", "channel", "1.2")).toBeUndefined();
		expect(slackConversation("T1", "", "channel", "1.2")).toBeUndefined();
		expect(slackConversation("T1", "C:1", "channel", "1.2")).toBeUndefined();
	});
});
