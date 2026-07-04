import { describe, it, expect } from "vitest";
import {
	buildConversation,
	parseConversation,
	slackChatType,
	slackConversation,
} from "../src/conversation";

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
	it("builds a threaded channel ref", () => {
		expect(slackConversation("T1", "C1", "channel", "12.34"))
			.toBe("slack:T1:channel:C1:thread:12.34");
	});

	it("builds a DM ref without a thread", () => {
		expect(slackConversation("T1", "D1", "im"))
			.toBe("slack:T1:dm:D1");
	});
});
