/**
 * Markdown tables -> Slack Block Kit `table` blocks that wrap (#1123).
 *
 * Slack builds its own table from `markdown_text`, but that table never sets
 * `column_settings[].is_wrapped`, so long cells widen the table instead of
 * wrapping. A message holding a table therefore goes out as blocks: the prose
 * around each table as `markdown` blocks (exact source slices, so it renders
 * through the same Slack translator as before), and each table as a `table`
 * block with every column wrapped and `rich_text` cells shaped like the ones
 * Slack's own conversion produces.
 *
 * Tables are found with a real GFM lexer (marked), never with regexes, and
 * only when some line looks like a delimiter row, so replies without a table
 * never pay for lexing. Anything this module cannot convert faithfully goes
 * out as `markdown_text`, byte-identical to before.
 */
import { Lexer, type Token, type Tokens } from "marked";

// Table block limits (docs.slack.dev table block). Rows include the header.
const MAX_ROWS = 100;
const MAX_COLUMNS = 20;
const MAX_CELL_CHARS = 10_000;

// Cheap pre-check: a line made only of pipes, colons, dashes, whitespace and
// blockquote markers, with at least one dash and one pipe or colon.
const DELIMITER_ROW = /^(?=[ \t>|:-]*-)(?=[ \t>|:-]*[|:])[ \t>|:-]+$/m;

export interface RichTextStyle {
	bold?: true;
	italic?: true;
	strike?: true;
	code?: true;
}

export type RichTextElement =
	| { type: "text"; text: string; style?: RichTextStyle }
	| { type: "link"; url: string; text?: string; style?: RichTextStyle }
	| { type: "user"; user_id: string; style?: RichTextStyle }
	| { type: "channel"; channel_id: string; style?: RichTextStyle }
	| { type: "usergroup"; usergroup_id: string; style?: RichTextStyle }
	| { type: "broadcast"; range: "here" | "channel" | "everyone"; style?: RichTextStyle }
	| { type: "emoji"; name: string };

export interface RichTextCell {
	type: "rich_text";
	elements: [{ type: "rich_text_section"; elements: RichTextElement[] }];
}

export interface ColumnSetting {
	is_wrapped: true;
	align?: "left" | "center" | "right";
}

export type SlackBlock =
	| { type: "markdown"; text: string }
	| { type: "table"; column_settings: ColumnSetting[]; rows: RichTextCell[][] };

/**
 * How one Slack message goes out. `none`: no table, send `markdown_text`.
 * `blocks`: send `blocks` with `text` as the notification and readback
 * fallback. `fallback`: a table that cannot convert; send `markdown_text`,
 * and `reason` says why (safe to log: it never carries message text).
 */
export type SlackTableConversion =
	| { kind: "none" }
	| { kind: "blocks"; blocks: SlackBlock[]; text: string }
	| { kind: "fallback"; reason: string };

function normalizeNewlines(text: string): string {
	return text.replace(/\r\n?/g, "\n");
}

/** Top-level tokens of `text` (already normalized), or null when no line can be a delimiter row. */
function lexTables(text: string): Token[] | null {
	if (!DELIMITER_ROW.test(text)) return null;
	try {
		return Lexer.lex(text, { gfm: true });
	} catch {
		return null; // a lexer bug must never fail a send; the message goes out as today
	}
}

/** Tables in `token`, counting tables nested in lists and blockquotes. */
function tableCount(token: Token): number {
	if (token.type === "table") return 1;
	const children = token.type === "list"
		? (token as Tokens.List).items
		: (token as Tokens.Generic).tokens;
	let n = 0;
	for (const child of children ?? []) n += tableCount(child as Token);
	return n;
}

/**
 * Cells in one raw table row, split exactly the way marked's `splitCells`
 * does but without truncating to the header width: marked drops cells past
 * the header, and a row that has them must not convert (they usually come
 * from Slack syntax such as `<#C1|general>`, whose pipe splits a cell).
 */
function rawCellCount(row: string): number {
	const marked = row.replace(/\|/g, (_pipe: string, offset: number, str: string) => {
		let escaped = false;
		for (let i = offset - 1; i >= 0 && str[i] === "\\"; i--) escaped = !escaped;
		return escaped ? "|" : " |";
	});
	const cells = marked.split(/ \|/);
	if (!cells[0].trim()) cells.shift();
	if (cells.length > 0 && !cells[cells.length - 1].trim()) cells.pop();
	return cells.length;
}

/** Why `table` cannot become a table block, or null when it can. */
function tableLimitFailure(table: Tokens.Table): string | null {
	const columns = table.header.length;
	if (table.rows.length + 1 > MAX_ROWS) return `table has ${table.rows.length + 1} rows (max ${MAX_ROWS})`;
	if (columns > MAX_COLUMNS) return `table has ${columns} columns (max ${MAX_COLUMNS})`;
	const bodyLines = table.raw.split("\n").slice(2);
	if (bodyLines.some((line) => rawCellCount(line) > columns)) return "a table row has more cells than its header";
	return null;
}

function cellChars(table: Tokens.Table): number {
	let n = 0;
	for (const cell of table.header) n += cell.text.length;
	for (const row of table.rows) for (const cell of row) n += cell.text.length;
	return n;
}

// --- cells -----------------------------------------------------------------

const NAMED_ENTITIES: Record<string, string> = {
	amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'", nbsp: " ",
};

/** Decode the HTML entities agents write; Slack's own conversion decodes them too. */
function decodeEntities(text: string): string {
	return text.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (match, ref: string) => {
		if (ref[0] !== "#") return NAMED_ENTITIES[ref.toLowerCase()] ?? match;
		const code = ref[1] === "x" || ref[1] === "X" ? parseInt(ref.slice(2), 16) : parseInt(ref.slice(1), 10);
		return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff)
			? String.fromCodePoint(code)
			: match;
	});
}

// Slack entity syntax and emoji shortcodes, which Slack's own conversion
// turns into mention, broadcast and emoji elements.
const SLACK_TOKEN = /<@([UW][A-Z0-9]+)>|<#([CG][A-Z0-9]+)>|<!subteam\^(S[A-Z0-9]+)>|<!(here|channel|everyone)>|:([a-z0-9_+'-]+):/g;
const NUMERIC_EMOJI = new Set(["+1", "-1", "100", "1234"]);
const isAlnum = (ch: string | undefined) => ch !== undefined && /[A-Za-z0-9]/.test(ch);

function withStyle<T extends RichTextElement>(element: T, style: RichTextStyle): T {
	return Object.keys(style).length > 0 ? { ...element, style: { ...style } } : element;
}

/**
 * A run of plain text (not code) as rich text elements. Slack syntax is
 * matched on the source before entities are decoded, so `&lt;@U1&gt;`
 * stays literal text, as it renders today. An emoji shortcode only counts
 * between non-alphanumerics, so times such as `10:30:45` stay text, and
 * adjacent shortcodes (`:wave::skin-tone-2:`) are each an emoji.
 */
function textElements(text: string, style: RichTextStyle): RichTextElement[] {
	const out: RichTextElement[] = [];
	let last = 0;
	let lastEmojiEnd = -1;
	const pushText = (s: string) => {
		if (s) out.push(withStyle({ type: "text", text: decodeEntities(s) }, style));
	};
	for (const m of text.matchAll(SLACK_TOKEN)) {
		const start = m.index;
		const end = start + m[0].length;
		let element: RichTextElement | null = null;
		if (m[1]) element = withStyle({ type: "user", user_id: m[1] }, style);
		else if (m[2]) element = withStyle({ type: "channel", channel_id: m[2] }, style);
		else if (m[3]) element = withStyle({ type: "usergroup", usergroup_id: m[3] }, style);
		else if (m[4]) element = withStyle({ type: "broadcast", range: m[4] as "here" | "channel" | "everyone" }, style);
		else {
			const name = m[5];
			const before = text[start - 1];
			const boundedBefore = start === 0 || start === lastEmojiEnd || (!isAlnum(before) && before !== ":");
			const plausible = /[a-z]/.test(name) || NUMERIC_EMOJI.has(name);
			if (boundedBefore && !isAlnum(text[end]) && plausible) {
				element = { type: "emoji", name };
				lastEmojiEnd = end;
			}
		}
		if (!element) continue;
		pushText(text.slice(last, start));
		out.push(element);
		last = end;
	}
	pushText(text.slice(last));
	return out;
}

type InlineRun =
	| { kind: "text"; text: string; style: RichTextStyle }
	| { kind: "element"; element: RichTextElement };

function sameStyle(a: RichTextStyle, b: RichTextStyle): boolean {
	return a.bold === b.bold && a.italic === b.italic && a.strike === b.strike && a.code === b.code;
}

/** Plain text of inline tokens, for link labels and readback. */
function plainText(tokens: Token[] | undefined): string {
	let out = "";
	for (const token of tokens ?? []) {
		const t = token as Tokens.Generic;
		if (t.type === "br") out += " ";
		else if (t.tokens && t.tokens.length > 0 && t.type !== "codespan") out += plainText(t.tokens);
		else if (t.type === "text" || t.type === "escape" || t.type === "codespan") out += decodeEntities(String(t.text));
		else out += t.raw;
	}
	return out;
}

function inlineRuns(tokens: Token[] | undefined, style: RichTextStyle, out: InlineRun[]): void {
	for (const token of tokens ?? []) {
		const t = token as Tokens.Generic;
		switch (t.type) {
		case "strong":
			inlineRuns(t.tokens, { ...style, bold: true }, out);
			break;
		case "em":
			inlineRuns(t.tokens, { ...style, italic: true }, out);
			break;
		case "del":
			inlineRuns(t.tokens, { ...style, strike: true }, out);
			break;
		case "codespan":
			out.push({ kind: "element", element: withStyle({ type: "text", text: String(t.text) }, { ...style, code: true }) });
			break;
		case "link": {
			// One link element per link, styled by what encloses it; a label's
			// own formatting is dropped, as Slack's conversion drops it.
			const link = t as Tokens.Link;
			const label = plainText(link.tokens);
			const element: RichTextElement = link.autolink
				? { type: "link", url: link.href }
				: { type: "link", url: link.href, text: label || link.href };
			out.push({ kind: "element", element: withStyle(element, style) });
			break;
		}
		case "image": {
			// Slack's conversion turns an image into a link to it.
			const image = t as Tokens.Image;
			const element: RichTextElement = image.text
				? { type: "link", url: image.href, text: image.text }
				: { type: "link", url: image.href };
			out.push({ kind: "element", element: withStyle(element, style) });
			break;
		}
		case "br":
			out.push({ kind: "text", text: "\n", style });
			break;
		case "escape":
			out.push({ kind: "text", text: String(t.text), style });
			break;
		case "text":
			if (t.tokens && t.tokens.length > 0) inlineRuns(t.tokens, style, out);
			else out.push({ kind: "text", text: String(t.text), style });
			break;
		default:
			// Inline HTML and anything else keep their source, as Slack does.
			out.push({ kind: "text", text: t.raw, style });
		}
	}
}

function cellElements(tokens: Token[]): RichTextElement[] {
	const runs: InlineRun[] = [];
	inlineRuns(tokens, {}, runs);
	// Merge neighbouring text runs first: the lexer splits text at `<` and
	// `!`, which would otherwise cut `<!here>` in two.
	const merged: InlineRun[] = [];
	for (const run of runs) {
		const prev = merged[merged.length - 1];
		if (run.kind === "text" && prev?.kind === "text" && sameStyle(prev.style, run.style)) {
			merged[merged.length - 1] = { ...prev, text: prev.text + run.text };
		} else {
			merged.push(run);
		}
	}
	const elements: RichTextElement[] = [];
	for (const run of merged) {
		if (run.kind === "element") elements.push(run.element);
		else elements.push(...textElements(run.text, run.style));
	}
	// An empty cell holds one space, the same shape Slack's conversion uses.
	return elements.length > 0 ? elements : [{ type: "text", text: " " }];
}

function richTextCell(cell: Tokens.TableCell): RichTextCell {
	return { type: "rich_text", elements: [{ type: "rich_text_section", elements: cellElements(cell.tokens) }] };
}

function tableBlock(table: Tokens.Table): SlackBlock {
	return {
		type: "table",
		column_settings: table.align.map((align) => (align ? { is_wrapped: true, align } : { is_wrapped: true })),
		rows: [table.header, ...table.rows].map((row) => row.map(richTextCell)),
	};
}

/** A table as plain rows for the fallback text: header first, cells joined by ` | `. */
function tableRows(table: Tokens.Table): string {
	const cellText = (cell: Tokens.TableCell) =>
		cellElements(cell.tokens).map((e) => {
			switch (e.type) {
			case "text": return e.text;
			case "link": return e.text && e.text !== e.url ? `${e.text} (${e.url})` : e.url;
			case "user": return `<@${e.user_id}>`;
			case "channel": return `<#${e.channel_id}>`;
			case "usergroup": return `<!subteam^${e.usergroup_id}>`;
			case "broadcast": return `<!${e.range}>`;
			case "emoji": return `:${e.name}:`;
			}
		}).join("").trim();
	return [table.header, ...table.rows].map((row) => row.map(cellText).join(" | ")).join("\n");
}

/** `s` without the blank lines that separate it from a neighbouring table. */
function trimBlankLines(s: string): string {
	return s.replace(/^(?:[ \t]*\n)+/, "").replace(/(?:\n[ \t]*)+$/, "");
}

/**
 * Decide how one Slack message goes out. Converts when every table in the
 * message is top-level, there are at most `maxTables` of them, and each fits
 * Slack's table limits; the cells of all tables together stay within 10,000
 * characters (Slack's per-message cap). Counts are taken on cell source,
 * which is never shorter than the visible text.
 */
export function convertSlackTables(text: string, maxTables: number): SlackTableConversion {
	const source = normalizeNewlines(text);
	const tokens = lexTables(source);
	if (!tokens) return { kind: "none" };
	const tables = tokens.reduce((n, token) => n + tableCount(token), 0);
	if (tables === 0) return { kind: "none" };
	const topLevel = tokens.filter((token): token is Tokens.Table => token.type === "table");
	if (topLevel.length !== tables) return { kind: "fallback", reason: "a table is nested in a list or quote" };
	if (tables > maxTables) return { kind: "fallback", reason: `${tables} tables in one message (max ${maxTables})` };
	for (const table of topLevel) {
		const failure = tableLimitFailure(table);
		if (failure) return { kind: "fallback", reason: failure };
	}
	const chars = topLevel.reduce((n, table) => n + cellChars(table), 0);
	if (chars > MAX_CELL_CHARS) return { kind: "fallback", reason: `table cells hold ${chars} characters (max ${MAX_CELL_CHARS})` };

	const blocks: SlackBlock[] = [];
	const fallback: string[] = [];
	const pushProse = (slice: string) => {
		const prose = trimBlankLines(slice);
		if (!prose.trim()) return;
		blocks.push({ type: "markdown", text: prose });
		fallback.push(prose);
	};
	let offset = 0;
	let proseStart = 0;
	for (const token of tokens) {
		const start = offset;
		offset += token.raw.length;
		if (token.type !== "table") continue;
		pushProse(source.slice(proseStart, start));
		blocks.push(tableBlock(token as Tokens.Table));
		fallback.push(tableRows(token as Tokens.Table));
		proseStart = offset;
	}
	pushProse(source.slice(proseStart));
	return { kind: "blocks", blocks, text: fallback.join("\n\n") };
}

// --- splitting a reply at tables --------------------------------------------

/** Heading, divider, or a paragraph that is bold from start to end. */
function isLeadToken(token: Token): boolean {
	if (token.type === "heading" || token.type === "hr") return true;
	if (token.type !== "paragraph") return false;
	const inline = ((token as Tokens.Paragraph).tokens ?? [])
		.filter((t) => !(t.type === "text" && !String((t as Tokens.Text).text).trim()));
	return inline.length === 1 && inline[0].type === "strong";
}

/**
 * Split `text` so each piece holds at most `maxTables` table-bearing
 * top-level tokens (a table, or a list or quote holding one). Between two
 * pieces the cut falls before the last unbroken run of lead tokens, so a
 * section's divider and heading travel with the table they introduce; with
 * no lead token, it falls right before the next table. Pieces are source
 * slices in order. Text needing no cut comes back whole, byte-identical.
 */
export function splitAtTables(text: string, maxTables: number): string[] {
	const source = normalizeNewlines(text);
	const tokens = lexTables(source);
	if (!tokens) return [text];
	const bearing = tokens.flatMap((token, i) => (tableCount(token) > 0 ? [i] : []));
	if (bearing.length <= maxTables) return [text];

	const starts: number[] = [];
	let offset = 0;
	for (const token of tokens) {
		starts.push(offset);
		offset += token.raw.length;
	}
	const cuts: number[] = [];
	for (let k = maxTables; k < bearing.length; k += maxTables) {
		const prev = bearing[k - 1];
		const next = bearing[k];
		let cut = next;
		let i = next - 1;
		while (i > prev && !isLeadToken(tokens[i])) i--; // skip to the end of the last lead run
		while (i > prev && (isLeadToken(tokens[i]) || tokens[i].type === "space")) {
			if (isLeadToken(tokens[i])) cut = i;
			i--;
		}
		cuts.push(starts[cut]);
	}
	const bounds = [0, ...cuts, source.length];
	const pieces: string[] = [];
	for (let p = 0; p + 1 < bounds.length; p++) {
		pieces.push(source.slice(bounds[p], bounds[p + 1]).replace(/\s+$/, ""));
	}
	return pieces;
}
