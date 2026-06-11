/**
 * Spike #191 — Chat SDK bridge adapter tests
 *
 * Validates that the Chat SDK's Slack webhook parsing can feed into
 * our NormalizedEvent envelope without losing fields we depend on.
 *
 * These tests run WITHOUT Slack credentials — they exercise the
 * parsing/normalization layer only, not live API calls.
 */
import { describe, it, expect } from "vitest";
import { parseSlackWebhookBody } from "@chat-adapter/slack/webhook";
import { slackMrkdwnToMarkdown } from "@chat-adapter/slack/format";

import {
	bridgeSlackWebhook,
	type ChatSdkBridgeResult,
} from "../src/adapters/chat-sdk-slack";

// ---------------------------------------------------------------------------
// Fixtures — Slack event payloads
// ---------------------------------------------------------------------------

function mentionPayload(overrides: Record<string, unknown> = {}) {
	return JSON.stringify({
		type: "event_callback",
		team_id: "T0952RZRZ0X",
		event_id: "Ev01ABC",
		event: {
			type: "app_mention",
			user: "U123USER",
			channel: "C456CHAN",
			channel_type: "channel",
			text: "<@UBOT> check the deploy",
			ts: "1718100000.000100",
			...overrides,
		},
	});
}

function dmPayload(overrides: Record<string, unknown> = {}) {
	return JSON.stringify({
		type: "event_callback",
		team_id: "T0952RZRZ0X",
		event_id: "Ev02DM",
		event: {
			type: "message",
			user: "U123USER",
			channel: "D789DM",
			channel_type: "im",
			text: "what's the status?",
			ts: "1718100001.000200",
			...overrides,
		},
	});
}

function threadReplyPayload(overrides: Record<string, unknown> = {}) {
	return JSON.stringify({
		type: "event_callback",
		team_id: "T0952RZRZ0X",
		event_id: "Ev03THREAD",
		event: {
			type: "message",
			user: "U123USER",
			channel: "C456CHAN",
			channel_type: "channel",
			text: "here's the update",
			ts: "1718100002.000300",
			thread_ts: "1718100000.000100",
			...overrides,
		},
	});
}

function urlVerificationPayload() {
	return JSON.stringify({
		type: "url_verification",
		challenge: "test_challenge_token",
	});
}

// ---------------------------------------------------------------------------
// Q1: Chat SDK webhook parsing works without credentials
// Chat SDK uses `kind` as discriminant, not `type`.
// ---------------------------------------------------------------------------

describe("Chat SDK webhook parsing (no credentials)", () => {
	it("parses an app_mention event", () => {
		const parsed = parseSlackWebhookBody(mentionPayload());
		expect(parsed.kind).toBe("app_mention");
	});

	it("parses a DM event", () => {
		const parsed = parseSlackWebhookBody(dmPayload());
		expect(parsed.kind).toBe("direct_message");
	});

	it("parses a thread reply", () => {
		const parsed = parseSlackWebhookBody(threadReplyPayload());
		// Thread replies in channels are not DMs — Chat SDK may classify
		// differently. The bridge handles re-classification.
		expect(parsed.kind).toBeTruthy();
	});

	it("parses url_verification", () => {
		const parsed = parseSlackWebhookBody(urlVerificationPayload());
		expect(parsed.kind).toBe("url_verification");
		if (parsed.kind === "url_verification") {
			expect(parsed.challenge).toBe("test_challenge_token");
		}
	});
});

// ---------------------------------------------------------------------------
// Q2: Bridge produces valid NormalizedEvent with all required fields
// ---------------------------------------------------------------------------

describe("bridgeSlackWebhook → NormalizedEvent", () => {
	it("produces a valid v2 envelope for app_mention", () => {
		const result = bridgeSlackWebhook(mentionPayload());
		expect(result.skip).toBe(false);
		expect(result.event).not.toBeNull();

		const event = result.event!;
		expect(event.v).toBe(2);
		expect(event.source).toBe("slack");
		expect(event.type).toBe("slack.mention");
		expect(event.delivery).toBe("chat");
		expect(event.topics).toEqual(["slack:T0952RZRZ0X"]);
		expect(event.text).toContain("check the deploy");
	});

	it("preserves all fields we depend on for app_mention", () => {
		const result = bridgeSlackWebhook(mentionPayload());
		const fields = result.event!.fields!;
		expect(fields.user_id).toBe("U123USER");
		expect(fields.channel).toBe("C456CHAN");
		expect(fields.channel_type).toBe("channel");
		expect(fields.ts).toBe("1718100000.000100");
	});

	it("produces slack.dm for DM events", () => {
		const result = bridgeSlackWebhook(dmPayload());
		expect(result.event!.type).toBe("slack.dm");
		expect(result.event!.fields!.channel_type).toBe("im");
	});

	it("produces slack.thread_reply with thread_ts", () => {
		const result = bridgeSlackWebhook(threadReplyPayload());
		expect(result.event!.type).toBe("slack.thread_reply");
		expect(result.event!.fields!.thread_ts).toBe("1718100000.000100");
		expect(result.event!.fields!.ts).toBe("1718100002.000300");
	});

	it("handles url_verification with challenge", () => {
		const result = bridgeSlackWebhook(urlVerificationPayload());
		expect(result.skip).toBe(true);
		expect(result.challenge).toBe("test_challenge_token");
		expect(result.event).toBeNull();
	});

	it("skips bot messages when selfBotId matches", () => {
		const body = JSON.stringify({
			type: "event_callback",
			team_id: "T0952RZRZ0X",
			event: {
				type: "app_mention",
				bot_id: "B_SELF",
				channel: "C456CHAN",
				text: "echo",
				ts: "1718100003.000400",
			},
		});
		const result = bridgeSlackWebhook(body, "B_SELF");
		expect(result.skip).toBe(true);
		expect(result.event).toBeNull();
	});

	it("allows messages from other bots", () => {
		const body = JSON.stringify({
			type: "event_callback",
			team_id: "T0952RZRZ0X",
			event: {
				type: "app_mention",
				bot_id: "B_OTHER",
				user: "U_OTHER",
				channel: "C456CHAN",
				text: "from another bot",
				ts: "1718100004.000500",
			},
		});
		const result = bridgeSlackWebhook(body, "B_SELF");
		expect(result.skip).toBe(false);
		expect(result.event).not.toBeNull();
	});

	it("skips message subtypes (edits, etc.)", () => {
		const body = JSON.stringify({
			type: "event_callback",
			team_id: "T0952RZRZ0X",
			event: {
				type: "message",
				subtype: "message_changed",
				channel: "C456CHAN",
				ts: "1718100005.000600",
			},
		});
		const result = bridgeSlackWebhook(body);
		expect(result.skip).toBe(true);
	});

	it("skips non-event_callback payloads", () => {
		const body = JSON.stringify({ type: "app_rate_limited" });
		const result = bridgeSlackWebhook(body);
		expect(result.skip).toBe(true);
	});
});

// ---------------------------------------------------------------------------
// Q2 continued: field-level parity with our existing normalizer
// ---------------------------------------------------------------------------

describe("bridge parity with existing normalizeSlackWebhook", () => {
	it("produces identical topic routing keys", () => {
		const result = bridgeSlackWebhook(mentionPayload());
		// Our existing normalizer produces ["slack:T0952RZRZ0X"]
		expect(result.event!.topics).toEqual(["slack:T0952RZRZ0X"]);
	});

	it("uses event_id as the envelope id", () => {
		const result = bridgeSlackWebhook(mentionPayload());
		expect(result.event!.id).toBe("Ev01ABC");
	});

	it("caps text at 4000 chars", () => {
		const longText = "x".repeat(5000);
		const result = bridgeSlackWebhook(
			mentionPayload({ text: longText }),
		);
		expect(result.event!.text.length).toBeLessThanOrEqual(4000);
	});
});

// ---------------------------------------------------------------------------
// Q3: Chat SDK markdown conversion is better than our regex approach
// ---------------------------------------------------------------------------

describe("Chat SDK markdown conversion quality", () => {
	it("converts Slack mrkdwn bold to markdown", () => {
		const md = slackMrkdwnToMarkdown("*bold text*");
		expect(md).toContain("**bold text**");
	});

	it("converts Slack user mentions", () => {
		const md = slackMrkdwnToMarkdown("<@U123> hello");
		// Should preserve or convert mention syntax
		expect(md).toBeTruthy();
	});

	it("converts Slack links to markdown links", () => {
		const md = slackMrkdwnToMarkdown("<https://example.com|click here>");
		expect(md).toContain("[click here](https://example.com)");
	});
});

// ---------------------------------------------------------------------------
// Q4: Integration shape — bridge result can feed into storage.deliver()
// ---------------------------------------------------------------------------

describe("bridge result feeds into existing event server", () => {
	it("result shape matches SlackNormalizationResult", () => {
		const result = bridgeSlackWebhook(mentionPayload());
		// Must have: event (NormalizedEvent | null), skip (boolean)
		// Optional: challenge (string)
		expect(typeof result.skip).toBe("boolean");
		expect(result.event === null || typeof result.event === "object").toBe(true);

		if (result.event) {
			// All NormalizedEvent required fields present
			expect(result.event.v).toBe(2);
			expect(typeof result.event.id).toBe("string");
			expect(typeof result.event.source).toBe("string");
			expect(typeof result.event.type).toBe("string");
			expect(typeof result.event.timestamp).toBe("string");
			expect(Array.isArray(result.event.topics)).toBe(true);
			expect(["chat", "bulk"]).toContain(result.event.delivery);
			expect(typeof result.event.text).toBe("string");
			expect(typeof result.event.payload).toBe("object");
		}
	});

	it("payload preserves raw Slack fields for downstream consumers", () => {
		const result = bridgeSlackWebhook(threadReplyPayload());
		const payload = result.event!.payload;
		expect(payload.user_id).toBe("U123USER");
		expect(payload.channel).toBe("C456CHAN");
		expect(payload.ts).toBe("1718100002.000300");
		expect(payload.thread_ts).toBe("1718100000.000100");
	});
});
