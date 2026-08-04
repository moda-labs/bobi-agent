import { describe, it, expect } from "vitest";
import { bridgeSlackWebhook } from "@moda-labs/bobi-events-core/adapters/chat-sdk-slack";

// Real-world shape: Zach replies in a thread the BOT authored, no @-mention.
const untaggedReplyOnBotThread = JSON.stringify({
	type: "event_callback",
	team_id: "T0952RZRZ0X",
	api_app_id: "A0BDLA833MW",
	event_id: "Ev_REPRO",
	event: {
		type: "message",
		user: "U0952RZTHBR",
		channel: "C0BAEN48KQR",
		channel_type: "channel",
		text: "yes go ahead",
		ts: "1785882670.615469",
		thread_ts: "1785881993.704639",
	},
});

describe("REPRO", () => {
	it("shows what the adapter does with an untagged thread reply", () => {
		const r = bridgeSlackWebhook(untaggedReplyOnBotThread, "B_SELF", "U0BCVME6Z60");
		console.log("SKIP:", r.skip);
		console.log("TYPE:", r.event?.type);
		console.log("TOPICS:", JSON.stringify(r.event?.topics));
		console.log("CONV:", r.event?.conversation);
		expect(r.skip).toBe(false);
	});
});
