import { describe, it, expect } from "vitest";
import {
	DiscordGatewaySession,
	DiscordIntents,
	DEFAULT_DISCORD_INTENTS,
	GatewayOp,
	type GatewayAction,
} from "@moda-labs/bobi-events-core/gateway/discord";
import { normalizeDiscordMessage } from "@moda-labs/bobi-events-core/adapters/discord";

const TOKEN = "bot-token-1";
const APP_ID = "111222333444555666";
const BOT_USER = "999888777666555444";

function session(opts: Partial<ConstructorParameters<typeof DiscordGatewaySession>[0]> = {}) {
	return new DiscordGatewaySession({
		token: TOKEN,
		applicationId: APP_ID,
		random: () => 0.5,
		...opts,
	});
}

function frame(op: number, d: unknown = null, extra: Record<string, unknown> = {}): string {
	return JSON.stringify({ op, d, ...extra });
}

function dispatch(t: string, d: Record<string, unknown>, s: number): string {
	return JSON.stringify({ op: GatewayOp.DISPATCH, t, d, s });
}

const HELLO = frame(GatewayOp.HELLO, { heartbeat_interval: 41250 });

const READY = dispatch("READY", {
	session_id: "sess-1",
	resume_gateway_url: "wss://resume.example",
	user: { id: BOT_USER },
	application: { id: APP_ID },
}, 1);

function sent(actions: GatewayAction[]): Record<string, unknown>[] {
	return actions
		.filter((a): a is Extract<GatewayAction, { kind: "send" }> => a.kind === "send")
		.map((a) => JSON.parse(a.frame));
}

/** A connected session (HELLO + IDENTIFY + READY replayed). */
function readySession() {
	const s = session();
	s.onFrame(HELLO);
	s.onFrame(READY);
	return s;
}

function guildMessage(over: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		id: "1300000000000000001",
		channel_id: "chan-1",
		guild_id: "guild-1",
		content: "hello",
		author: { id: "user-1", username: "ada" },
		mentions: [],
		...over,
	};
}

describe("DiscordGatewaySession handshake", () => {
	it("responds to HELLO with a jittered heartbeat schedule and IDENTIFY", () => {
		const s = session();
		const actions = s.onFrame(HELLO);

		expect(actions[0]).toEqual({
			kind: "setHeartbeat", intervalMs: 41250, firstDelayMs: 20625,
		});
		const [identify] = sent(actions);
		expect(identify.op).toBe(GatewayOp.IDENTIFY);
		const d = identify.d as Record<string, unknown>;
		expect(d.token).toBe(TOKEN);
		expect(d.intents).toBe(DEFAULT_DISCORD_INTENTS);
		// Single shard by design (v1 refuses sharded bots at the driver).
		expect(d.shard).toEqual([0, 1]);
		// MESSAGE_CONTENT is privileged and strictly opt-in - identifying with
		// it un-enabled closes the socket with 4014.
		expect((d.intents as number) & DiscordIntents.MESSAGE_CONTENT).toBe(0);
	});

	it("RESUMEs instead of IDENTIFYing when constructed with resume state", () => {
		const s = session({
			resume: { sessionId: "sess-1", resumeGatewayUrl: "wss://resume.example", lastSeq: 42 },
		});
		const [resume] = sent(s.onFrame(HELLO));
		expect(resume.op).toBe(GatewayOp.RESUME);
		expect(resume.d).toEqual({ token: TOKEN, session_id: "sess-1", seq: 42 });
	});

	it("records session state and the bot user from READY", () => {
		const s = session();
		s.onFrame(HELLO);
		const actions = s.onFrame(READY);
		expect(actions).toEqual([{ kind: "connected", resumed: false }]);
		expect(s.isReady()).toBe(true);
		expect(s.botUserId()).toBe(BOT_USER);
		expect(s.state()).toEqual({
			sessionId: "sess-1", resumeGatewayUrl: "wss://resume.example", lastSeq: 1,
		});
	});

	it("signals connected on RESUMED", () => {
		const s = session({
			resume: { sessionId: "sess-1", resumeGatewayUrl: "wss://r", lastSeq: 42 },
		});
		s.onFrame(HELLO);
		expect(s.onFrame(dispatch("RESUMED", {}, 43))).toEqual([
			{ kind: "connected", resumed: true },
		]);
	});

	it("ignores malformed frames instead of dying", () => {
		const s = readySession();
		expect(s.onFrame("not json {")).toEqual([]);
		expect(s.onFrame(JSON.stringify(null))).toEqual([]);
		expect(s.onFrame(frame(12345))).toEqual([]);
	});
});

describe("DiscordGatewaySession heartbeats", () => {
	it("beats with the last seen sequence number and expects an ACK", () => {
		const s = readySession();
		s.onFrame(dispatch("MESSAGE_CREATE", guildMessage(), 7));

		const [beat] = sent(s.onTimer("heartbeat"));
		expect(beat).toEqual({ op: GatewayOp.HEARTBEAT, d: 7 });

		// ACK arrives - the next timer beats again instead of reconnecting.
		s.onFrame(frame(GatewayOp.HEARTBEAT_ACK));
		expect(sent(s.onTimer("heartbeat"))).toHaveLength(1);
	});

	it("treats a missed ACK as a zombie connection and resumes", () => {
		const s = readySession();
		s.onTimer("heartbeat");
		// No ACK before the next timer fires: reconnect, resume-first.
		expect(s.onTimer("heartbeat")).toEqual([{ kind: "reconnect", resume: true }]);
	});

	it("answers a server-requested heartbeat immediately", () => {
		const s = readySession();
		const [beat] = sent(s.onFrame(frame(GatewayOp.HEARTBEAT)));
		expect(beat.op).toBe(GatewayOp.HEARTBEAT);
	});
});

describe("DiscordGatewaySession reconnect policy", () => {
	it("resumes on RECONNECT (op 7)", () => {
		const s = readySession();
		expect(s.onFrame(frame(GatewayOp.RECONNECT))).toEqual([
			{ kind: "reconnect", resume: true },
		]);
	});

	it("re-IDENTIFYs on a non-resumable INVALID_SESSION and clears state", () => {
		const s = readySession();
		expect(s.onFrame(frame(GatewayOp.INVALID_SESSION, false))).toEqual([
			{ kind: "reconnect", resume: false },
		]);
		expect(s.state()).toBeNull();
	});

	it("resumes on a resumable INVALID_SESSION", () => {
		const s = readySession();
		expect(s.onFrame(frame(GatewayOp.INVALID_SESSION, true))).toEqual([
			{ kind: "reconnect", resume: true },
		]);
		expect(s.state()).not.toBeNull();
	});

	it("marks 4004/4013/4014 closes fatal - no reconnect, ever", () => {
		for (const code of [4004, 4013, 4014]) {
			const s = readySession();
			const actions = s.onSocketClose(code);
			expect(actions).toHaveLength(1);
			expect(actions[0].kind).toBe("fatal");
			expect((actions[0] as { reason: string }).reason).toContain(String(code));
		}
	});

	it("resumes on an ordinary abnormal close, re-IDENTIFYs on session-ending closes", () => {
		const s = readySession();
		expect(s.onSocketClose(1006)).toEqual([{ kind: "reconnect", resume: true }]);

		// Clean closes and 4007/4009 invalidate the session.
		for (const code of [1000, 4007, 4009]) {
			const s2 = readySession();
			expect(s2.onSocketClose(code)).toEqual([{ kind: "reconnect", resume: false }]);
			expect(s2.state()).toBeNull();
		}
	});

	it("never resumes before a session exists", () => {
		const s = session();
		s.onFrame(HELLO);
		expect(s.onSocketClose(1006)).toEqual([{ kind: "reconnect", resume: false }]);
	});
});

describe("DiscordGatewaySession message delivery", () => {
	it("delivers a DM as discord.dm with a dm conversation ref", () => {
		const s = readySession();
		const actions = s.onFrame(dispatch("MESSAGE_CREATE", {
			id: "1300000000000000002",
			channel_id: "dm-chan",
			content: "hi bot",
			author: { id: "user-1", username: "ada" },
		}, 2));

		expect(actions).toHaveLength(1);
		expect(actions[0].kind).toBe("deliver");
		const [event] = (actions[0] as Extract<GatewayAction, { kind: "deliver" }>).events;
		expect(event.type).toBe("discord.dm");
		expect(event.id).toBe("1300000000000000002");
		expect(event.topics).toEqual([`discord:${APP_ID}`]);
		expect(event.delivery).toBe("chat");
		expect(event.text).toBe("hi bot");
		expect(event.conversation).toBe(`discord:${APP_ID}:dm:dm-chan`);
		expect(event.fields).toMatchObject({ user_id: "user-1", user_name: "ada" });
	});

	it("delivers a guild @mention as discord.mention with a channel ref", () => {
		const s = readySession();
		const actions = s.onFrame(dispatch("MESSAGE_CREATE", guildMessage({
			content: `<@${BOT_USER}> deploy please`,
			mentions: [{ id: BOT_USER }],
		}), 2));

		const [event] = (actions[0] as Extract<GatewayAction, { kind: "deliver" }>).events;
		expect(event.type).toBe("discord.mention");
		expect(event.conversation).toBe(`discord:${APP_ID}:channel:chan-1`);
		expect(event.fields?.guild_id).toBe("guild-1");
	});

	it("classifies a reply to the bot as discord.reply even with the auto-mention", () => {
		const s = readySession();
		const actions = s.onFrame(dispatch("MESSAGE_CREATE", guildMessage({
			content: "sounds good",
			mentions: [{ id: BOT_USER }], // Discord auto-mentions the replied-to user
			message_reference: { message_id: "prev-1" },
			referenced_message: { id: "prev-1", author: { id: BOT_USER } },
		}), 2));

		const [event] = (actions[0] as Extract<GatewayAction, { kind: "deliver" }>).events;
		expect(event.type).toBe("discord.reply");
	});

	it("drops the guild firehose: unaddressed messages yield nothing", () => {
		const s = readySession();
		expect(s.onFrame(dispatch("MESSAGE_CREATE", guildMessage(), 2))).toEqual([]);
	});

	it("drops bot-authored messages, including our own (loop prevention)", () => {
		const s = readySession();
		for (const author of [
			{ id: BOT_USER, username: "us", bot: true },
			{ id: "other-bot", username: "them", bot: true },
		]) {
			const actions = s.onFrame(dispatch("MESSAGE_CREATE", guildMessage({
				author, mentions: [{ id: BOT_USER }],
			}), 3));
			expect(actions).toEqual([]);
		}
	});
});

describe("normalizeDiscordMessage", () => {
	it("surfaces attachments in fields.files", () => {
		const [event] = normalizeDiscordMessage({
			id: "m1", channel_id: "c1", content: "see attached",
			author: { id: "u1", username: "ada" },
			attachments: [{
				id: "a1", filename: "report.pdf", content_type: "application/pdf",
				url: "https://cdn.discordapp.com/a1/report.pdf", size: 1234,
			}],
		}, BOT_USER, APP_ID);

		expect(JSON.parse(event.fields!.files as string)).toEqual([{
			id: "a1", name: "report.pdf", mimetype: "application/pdf",
			url: "https://cdn.discordapp.com/a1/report.pdf", size: "1234",
		}]);
	});

	it("marks withheld content instead of shipping an empty text", () => {
		// A guild reply whose auto-mention was suppressed loses content without
		// the privileged MESSAGE_CONTENT intent.
		const [event] = normalizeDiscordMessage({
			id: "m1", channel_id: "c1", guild_id: "g1", content: "",
			author: { id: "u1", username: "ada" },
			referenced_message: { author: { id: BOT_USER } },
		}, BOT_USER, APP_ID);
		expect(event.type).toBe("discord.reply");
		expect(event.text).toContain("Message Content intent");
	});

	it("prefers the display name over the username", () => {
		const [event] = normalizeDiscordMessage({
			id: "m1", channel_id: "c1", content: "hi",
			author: { id: "u1", username: "ada_l", global_name: "Ada Lovelace" },
		}, BOT_USER, APP_ID);
		expect(event.fields?.user_name).toBe("Ada Lovelace");
	});

	it("drops malformed payloads without throwing", () => {
		expect(normalizeDiscordMessage({}, BOT_USER, APP_ID)).toEqual([]);
		expect(normalizeDiscordMessage(
			{ id: "m", channel_id: "c" }, BOT_USER, APP_ID)).toEqual([]);
		// An id carrying ":" cannot be addressed by the conversation grammar.
		expect(normalizeDiscordMessage({
			id: "m", channel_id: "c:1", content: "x",
			author: { id: "u1", username: "a" },
		}, BOT_USER, APP_ID)).toEqual([]);
	});
});
