import { describe, it, expect } from "vitest";
import {
	convertSlackTables,
	splitAtTables,
	type RichTextElement,
	type SlackBlock,
} from "@moda-labs/bobi-events-core/markdown-tables";
import { chunkForChannel, getChannelAdapter } from "@moda-labs/bobi-events-core/channels";

function blocksOf(text: string): SlackBlock[] {
	const result = convertSlackTables(text, 1);
	if (result.kind !== "blocks") throw new Error(`expected blocks, got ${JSON.stringify(result)}`);
	return result.blocks;
}

function tableOf(text: string) {
	const table = blocksOf(text).find((b) => b.type === "table");
	if (!table || table.type !== "table") throw new Error("no table block");
	return table;
}

/** Elements of the single body cell of a one-column table holding `cell`. */
function cell(markdown: string): RichTextElement[] {
	return tableOf(`| h |\n|---|\n| ${markdown} |`).rows[1][0].elements[0].elements;
}

const reason = (text: string) => {
	const result = convertSlackTables(text, 1);
	return result.kind === "fallback" ? result.reason : result.kind;
};

describe("convertSlackTables", () => {
	it("puts the table between markdown blocks holding the exact prose around it", () => {
		const text = "# Title\n\nIntro with **bold** and a [link](https://a.example).\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- after one\n- after two";
		const blocks = blocksOf(text);
		expect(blocks.map((b) => b.type)).toEqual(["markdown", "table", "markdown"]);
		expect(blocks[0]).toEqual({ type: "markdown", text: "# Title\n\nIntro with **bold** and a [link](https://a.example)." });
		expect(blocks[2]).toEqual({ type: "markdown", text: "- after one\n- after two" });
	});

	it("omits a blank markdown block before or after the table", () => {
		expect(blocksOf("| a |\n|---|\n| 1 |").map((b) => b.type)).toEqual(["table"]);
		expect(blocksOf("\n\n| a |\n|---|\n| 1 |\n\n  \n").map((b) => b.type)).toEqual(["table"]);
	});

	it("wraps every column and takes alignment from the delimiter row", () => {
		const table = tableOf("| a | b | c | d |\n|---|:--|:-:|--:|\n| 1 | 2 | 3 | 4 |");
		expect(table.column_settings).toEqual([
			{ is_wrapped: true },
			{ is_wrapped: true, align: "left" },
			{ is_wrapped: true, align: "center" },
			{ is_wrapped: true, align: "right" },
		]);
	});

	it("keeps the header as the first row and pads short rows with a space", () => {
		const table = tableOf("| a | b |\n|---|---|\n| only |\n| x | |");
		expect(table.rows.map((r) => r.map((c) => c.elements[0].elements))).toEqual([
			[[{ type: "text", text: "a" }], [{ type: "text", text: "b" }]],
			[[{ type: "text", text: "only" }], [{ type: "text", text: " " }]],
			[[{ type: "text", text: "x" }], [{ type: "text", text: " " }]],
		]);
	});

	it("maps inline formatting the way Slack's own conversion does", () => {
		expect(cell("**b** _i_ ~~s~~ `c`")).toEqual([
			{ type: "text", text: "b", style: { bold: true } },
			{ type: "text", text: " " },
			{ type: "text", text: "i", style: { italic: true } },
			{ type: "text", text: " " },
			{ type: "text", text: "s", style: { strike: true } },
			{ type: "text", text: " " },
			{ type: "text", text: "c", style: { code: true } },
		]);
		expect(cell("***both***")).toEqual([{ type: "text", text: "both", style: { bold: true, italic: true } }]);
	});

	it("maps links: labelled, styled by what encloses them, bare and autolinked", () => {
		expect(cell("[PR #1](https://x.example/1)")).toEqual([{ type: "link", url: "https://x.example/1", text: "PR #1" }]);
		expect(cell("**[bold](https://x.example/b)**")).toEqual([
			{ type: "link", url: "https://x.example/b", text: "bold", style: { bold: true } },
		]);
		// A label's own formatting is dropped, as in Slack's conversion.
		expect(cell("[**mixed** label](https://x.example/m)")).toEqual([
			{ type: "link", url: "https://x.example/m", text: "mixed label" },
		]);
		expect(cell("https://x.example/bare <https://x.example/auto> <a@x.example>")).toEqual([
			{ type: "link", url: "https://x.example/bare" },
			{ type: "text", text: " " },
			{ type: "link", url: "https://x.example/auto" },
			{ type: "text", text: " " },
			{ type: "link", url: "mailto:a@x.example" },
		]);
		expect(cell("![logo](https://x.example/l.png)")).toEqual([{ type: "link", url: "https://x.example/l.png", text: "logo" }]);
	});

	it("maps Slack mentions and broadcasts, including inside formatting", () => {
		expect(cell("<@U123> <#C456> <!subteam^S789> <!here> <!channel> <!everyone>")).toEqual([
			{ type: "user", user_id: "U123" },
			{ type: "text", text: " " },
			{ type: "channel", channel_id: "C456" },
			{ type: "text", text: " " },
			{ type: "usergroup", usergroup_id: "S789" },
			{ type: "text", text: " " },
			{ type: "broadcast", range: "here" },
			{ type: "text", text: " " },
			{ type: "broadcast", range: "channel" },
			{ type: "text", text: " " },
			{ type: "broadcast", range: "everyone" },
		]);
		expect(cell("**hi <@U1>**")).toEqual([
			{ type: "text", text: "hi ", style: { bold: true } },
			{ type: "user", user_id: "U1", style: { bold: true } },
		]);
		// Code is literal, and entity-escaped syntax stays text, as in Slack's
		// own conversion (checked 2026-10-19), which still reads a backslash
		// escape as a broadcast.
		expect(cell("`<@U1>` &lt;@U2&gt; &#60;!channel&#62; &#x3C;@U3&#x3E;")).toEqual([
			{ type: "text", text: "<@U1>", style: { code: true } },
			{ type: "text", text: " <@U2> <!channel> <@U3>" },
		]);
		expect(cell("\\<!channel>")).toEqual([{ type: "broadcast", range: "channel" }]);
	});

	it("matches emoji shortcodes conservatively", () => {
		expect(cell(":white_check_mark: at 10:30:45 :wave::skin-tone-2: :+1:")).toEqual([
			{ type: "emoji", name: "white_check_mark" },
			{ type: "text", text: " at 10:30:45 " },
			{ type: "emoji", name: "wave" },
			{ type: "emoji", name: "skin-tone-2" },
			{ type: "text", text: " " },
			{ type: "emoji", name: "+1" },
		]);
		expect(cell("ratio 3:2:1 and a:b:c and :12:")).toEqual([{ type: "text", text: "ratio 3:2:1 and a:b:c and :12:" }]);
		// A shortcode glued to a word stays text, as in Slack's conversion.
		expect(cell("word:smile: (:smile:)")).toEqual([
			{ type: "text", text: "word:smile: (" },
			{ type: "emoji", name: "smile" },
			{ type: "text", text: ")" },
		]);
	});

	it("decodes entities, unescapes pipes and keeps inline HTML literal", () => {
		expect(cell("a &amp; b &#x41;&#66; x \\| y <b>raw</b>")).toEqual([
			{ type: "text", text: "a & b AB x | y <b>raw</b>" },
		]);
		expect(cell("a &mdash; b &rarr; c &constructor; &unknown;")).toEqual([
			{ type: "text", text: "a \u2014 b \u2192 c &constructor; &unknown;" },
		]);
	});

	it("writes the fallback text as prose plus plain table rows", () => {
		const result = convertSlackTables(
			"Before.\n\n| Name | Link |\n|---|---|\n| **Bold** | [PR](https://x.example/1) |\n| <@U1> | https://x.example/2 |\n\nAfter.", 1);
		expect(result.kind === "blocks" && result.text).toBe(
			"Before.\n\nName | Link\nBold | PR (https://x.example/1)\n<@U1> | https://x.example/2\n\nAfter.");
	});

	it("escapes literal mrkdwn characters in the fallback but keeps real mentions", () => {
		const result = convertSlackTables("| who | note |\n|---|---|\n| <@U1> | `<@U2>` &lt;!here&gt; a & b |", 1);
		expect(result.kind === "blocks" && result.text).toBe(
			"who | note\n<@U1> | &lt;@U2&gt; &lt;!here&gt; a &amp; b");
	});

	it("leaves a message alone when marked's tokens do not cover its source", () => {
		// marked drops a repeated link definition from the token list, which
		// would shift every offset after it.
		const text = "[a]: https://one.example\n[a]: https://two.example\n\nIntro.\n\n| h | k |\n|---|---|\n| c1 | c2 |\n\nOutro.";
		expect(convertSlackTables(text, 1)).toEqual({ kind: "none" });
		expect(splitAtTables(`${text}\n\n| t2 |\n|---|\n| x |`, 1)).toEqual([`${text}\n\n| t2 |\n|---|\n| x |`]);
	});

	it("converts a table whose rows have no outer pipes", () => {
		expect(tableOf("a | b\n--- | ---\n1 | 2").rows[1].map((c) => c.elements[0].elements)).toEqual([
			[{ type: "text", text: "1" }], [{ type: "text", text: "2" }],
		]);
	});

	it("converts CRLF input", () => {
		expect(blocksOf("x\r\n\r\n| a |\r\n|---|\r\n| 1 |\r\n").map((b) => b.type)).toEqual(["markdown", "table"]);
	});

	it("enforces each Slack table limit at its exact boundary", () => {
		const rows = (n: number) => ["| k |", "|---|", ...Array.from({ length: n - 1 }, (_, i) => `| ${i} |`)].join("\n");
		expect(reason(rows(100))).toBe("blocks");
		expect(reason(rows(101))).toBe("table has 101 rows (max 100)");

		const cols = (n: number) => {
			const line = (s: string) => `|${Array.from({ length: n }, () => ` ${s} `).join("|")}|`;
			return [line("h"), line("---"), line("v")].join("\n");
		};
		expect(reason(cols(20))).toBe("blocks");
		expect(reason(cols(21))).toBe("table has 21 columns (max 20)");

		// 10,000 characters across all cells, counted on cell source.
		const chars = (n: number) => `| ${"h".repeat(5000)} |\n|---|\n| ${"v".repeat(n - 5000)} |`;
		expect(reason(chars(10_000))).toBe("blocks");
		expect(reason(chars(10_001))).toBe("table cells hold 10001 characters (max 10000)");
	});

	it("refuses a row wider than its header instead of dropping cells", () => {
		expect(reason("| a | b |\n|---|---|\n| 1 | 2 | 3 |")).toBe("a table row has more cells than its header");
		// Slack's own link syntax carries a pipe that splits a cell.
		expect(reason("| who | where |\n|---|---|\n| me | <#C1|general> |")).toBe("a table row has more cells than its header");
		expect(reason("| who | where |\n|---|---|\n| me | x \\| y |")).toBe("blocks");
	});

	it("leaves messages without a convertible top-level table alone", () => {
		expect(reason("plain text with a | pipe")).toBe("none");
		expect(reason("```\n| a | b |\n|---|---|\n| 1 | 2 |\n```")).toBe("none");
		expect(reason("- item\n\n  | a | b |\n  |---|---|\n  | 1 | 2 |")).toBe("a table is nested in a list or quote");
		expect(reason("> | a | b |\n> |---|---|\n> | 1 | 2 |")).toBe("a table is nested in a list or quote");
		expect(reason("| a |\n|---|\n| 1 |\n\nmid\n\n| b |\n|---|\n| 2 |")).toBe("2 tables in one message (max 1)");
		// A nested table counts toward the cap even beside a top-level one.
		expect(reason("| a |\n|---|\n| 1 |\n\n> | b |\n> |---|\n> | 2 |")).toBe("a table is nested in a list or quote");
	});
});

describe("splitAtTables", () => {
	const table = (name: string) => `| ${name} | v |\n|---|---|\n| 1 | 2 |`;

	it("returns a reply with at most one table whole and byte-identical", () => {
		for (const text of ["no table", `intro\r\n\r\n${table("A")}\r\n`, "```\n| a |\n|---|\n```\n\n" + table("A")]) {
			expect(splitAtTables(text, 1)).toEqual([text]);
		}
	});

	it("cuts the standup shape before each section's divider", () => {
		const standup = [
			"*Standup, Oct 13*",
			"",
			"---",
			"**Project A**",
			"",
			"One-sentence takeaway for A.",
			"",
			table("A"),
			"",
			"Next steps:",
			"- step one",
			"",
			"---",
			"",
			"**Project B**",
			"",
			"Takeaway for B.",
			"",
			table("B"),
		].join("\n");
		expect(splitAtTables(standup, 1)).toEqual([
			["*Standup, Oct 13*", "", "---", "**Project A**", "", "One-sentence takeaway for A.", "", table("A"), "", "Next steps:", "- step one"].join("\n"),
			["---", "", "**Project B**", "", "Takeaway for B.", "", table("B")].join("\n"),
		]);
	});

	it("cuts right before the next table when nothing between them leads into it", () => {
		expect(splitAtTables(`${table("A")}\n\n${table("B")}`, 1)).toEqual([table("A"), table("B")]);
		// A paragraph that only starts bold is commentary, not a lead-in.
		expect(splitAtTables(`${table("A")}\n\n**Note:** about A.\n\n${table("B")}`, 1)).toEqual([
			`${table("A")}\n\n**Note:** about A.`,
			table("B"),
		]);
	});

	it("cuts before a heading and keeps a run of leads together", () => {
		expect(splitAtTables(`${table("A")}\n\nprose\n\n## B\n\n**Bold lead**\n\n${table("B")}`, 1)).toEqual([
			`${table("A")}\n\nprose`,
			`## B\n\n**Bold lead**\n\n${table("B")}`,
		]);
	});

	it("splits CRLF input at the same places as LF input", () => {
		const lf = `${table("A")}\n\n**B**\n\n${table("B")}`;
		expect(splitAtTables(lf.replace(/\n/g, "\r\n"), 1)).toEqual(splitAtTables(lf, 1));
		expect(splitAtTables(lf, 1)).toEqual([table("A"), `**B**\n\n${table("B")}`]);
	});

	it("treats a list holding a table as a table for the cut", () => {
		const nested = "- item\n\n  | n | v |\n  |---|---|\n  | 1 | 2 |";
		expect(splitAtTables(`${table("A")}\n\n${nested}`, 1)).toEqual([table("A"), nested]);
	});

	it("stays fast on a long whitespace run between tables", () => {
		const text = `${table("A")}\n\n${" ".repeat(200_000)}x\n\n${table("B")}`;
		const started = Date.now();
		expect(splitAtTables(text, 1)).toHaveLength(2);
		// The old whole-reply lex plus a backtracking trim took about 25 s here.
		expect(Date.now() - started).toBeLessThan(5_000);
	});

	it("never cuts inside one token holding two tables", () => {
		const quote = `> ${table("A").split("\n").join("\n> ")}\n>\n> ${table("B").split("\n").join("\n> ")}`;
		expect(splitAtTables(quote, 1)).toEqual([quote]);
	});
});

describe("chunkForChannel with Slack's one-table cap", () => {
	const slack = getChannelAdapter("slack")!.descriptor.capabilities;
	const tables = (n: number) =>
		Array.from({ length: n }, (_, i) => `**T${i + 1}**\n\n| T${i + 1} | v |\n|---|---|\n| ${i} | x |`).join("\n\n");

	it("declares the cap on Slack only", () => {
		expect(slack.maxTablesPerMessage).toBe(1);
		expect(getChannelAdapter("discord")!.descriptor.capabilities.maxTablesPerMessage).toBeUndefined();
		expect(getChannelAdapter("whatsapp")!.descriptor.capabilities.maxTablesPerMessage).toBeUndefined();
	});

	it("posts one message per table, in source order, with nothing lost", () => {
		const chunks = chunkForChannel(tables(3), slack);
		expect(chunks).toEqual([1, 2, 3].map((i) => `**T${i}**\n\n| T${i} | v |\n|---|---|\n| ${i - 1} | x |`));
	});

	it("counts the 8-message cap across pieces: 8 tables stay 8 messages", () => {
		const chunks = chunkForChannel(tables(8), slack);
		expect(chunks).toHaveLength(8);
		expect(chunks.every((c) => convertSlackTables(c, 1).kind === "blocks")).toBe(true);
	});

	it("joins the tables past message 7 into message 8, which goes out as markdown_text", () => {
		for (const n of [9, 10]) {
			const chunks = chunkForChannel(tables(n), slack);
			expect(chunks).toHaveLength(8);
			expect(chunks.slice(0, 7).every((c) => convertSlackTables(c, 1).kind === "blocks")).toBe(true);
			expect(convertSlackTables(chunks[7], 1)).toEqual({ kind: "fallback", reason: `${n - 7} tables in one message (max 1)` });
			// Joined pieces keep the blank line that separates them in the source.
			expect(chunks[7]).toContain("| 7 | x |\n\n**T9**");
			// Content survives in order across all eight messages.
			const joined = chunks.join("\n\n");
			const order = Array.from({ length: n }, (_, i) => joined.indexOf(`| T${i + 1} | v |`));
			expect(order.every((at, i) => at >= 0 && (i === 0 || at > order[i - 1]))).toBe(true);
		}
	});

	it("still length-chunks a long piece, keeping the table header with its first part", () => {
		// 20 narrow columns: the piece passes 12,000 characters while each
		// chunk's cells stay under Slack's 10,000-character cell cap.
		const line = (cell: string) => `|${Array.from({ length: 20 }, () => ` ${cell} `).join("|")}|`;
		const rows = Array.from({ length: 99 }, (_, i) => line(String(i).padStart(5, "w"))).join("\n");
		const long = `${line("head")}\n${line("---")}\n${rows}`;
		expect(long.length).toBeGreaterThan(12000);
		const chunks = chunkForChannel(`${long}\n\n${tables(1)}`, slack);
		expect(chunks.length).toBeGreaterThan(2);
		for (const c of chunks) expect(c.length).toBeLessThanOrEqual(12000);
		expect(convertSlackTables(chunks[0], 1).kind).toBe("blocks");
		expect(chunks[chunks.length - 1]).toBe(tables(1));
	});

	it("leaves Discord and WhatsApp chunking of multi-table text byte-identical", () => {
		for (const name of ["discord", "whatsapp"]) {
			const caps = getChannelAdapter(name)!.descriptor.capabilities;
			const text = tables(3);
			expect(chunkForChannel(text, caps)).toEqual([text]);
		}
	});
});
