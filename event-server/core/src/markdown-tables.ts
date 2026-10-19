/**
 * Markdown tables -> Slack Block Kit `table` blocks that wrap (#1123).
 *
 * Slack builds its own table from `markdown_text`, but that table never sets
 * `column_settings[].is_wrapped`, so long cells widen the table instead of
 * wrapping. A message holding a table therefore goes out as blocks: the prose
 * around each table as `markdown` blocks (exact source slices, so it renders
 * through the same Slack translator as before), and each table as a `table`
 * block with every column wrapped and `rich_text` cells shaped like the ones
 * Slack's own conversion produces (checked against it on 2026-10-19).
 *
 * Tables are found with a real GFM lexer (marked), never with regexes, and
 * only when some line looks like a delimiter row, so replies without a table
 * never pay for lexing. Anything this module cannot convert faithfully goes
 * out as `markdown_text`, byte-identical to before.
 */
import { Lexer, type MarkedToken, type Token, type Tokens } from "marked";

// Table block limits (docs.slack.dev table block). Rows include the header.
const MAX_ROWS = 100;
const MAX_COLUMNS = 20;
const MAX_CELL_CHARS = 10_000;

// Cheap pre-check: a line made only of pipes, colons, dashes, whitespace and
// blockquote markers, with at least one dash and one pipe or colon.
const DELIMITER_ROW = /^(?=[ \t>|:-]*-)(?=[ \t>|:-]*[|:])[ \t>|:-]+$/m;

// A paragraph longer than this is never a lead-in heading, so splitting
// never lexes long paragraphs inline.
const MAX_LEAD_CHARS = 300;

export interface RichTextStyle {
	bold?: true;
	italic?: true;
	strike?: true;
	code?: true;
}

type BroadcastRange = "here" | "channel" | "everyone";

export type RichTextElement =
	| { type: "text"; text: string; style?: RichTextStyle }
	| { type: "link"; url: string; text?: string; style?: RichTextStyle }
	| { type: "user"; user_id: string; style?: RichTextStyle }
	| { type: "channel"; channel_id: string; style?: RichTextStyle }
	| { type: "usergroup"; usergroup_id: string; style?: RichTextStyle }
	| { type: "broadcast"; range: BroadcastRange; style?: RichTextStyle }
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

/**
 * Block-level lexing of `source` (newlines already normalized). Only the
 * block structure is lexed here, which is cheap; inline lexing, the costly
 * part on long input, is left to the few table cells that need it. Every
 * offset below is a running sum of token `raw`s, so the tokens must spell
 * the source exactly, and marked does not always: it drops a repeated link
 * definition, and a quote holding a list with lazy continuation lines gets
 * a `raw` that differs from the source.
 */
type Lexed =
	| { kind: "no-table" }
	| { kind: "uncovered" }
	| { kind: "lexed"; lexer: Lexer; tokens: Token[] };

function lexBlocks(source: string): Lexed {
	if (!DELIMITER_ROW.test(source)) return { kind: "no-table" };
	const lexer = new Lexer({ gfm: true });
	let tokens: Token[];
	try {
		tokens = lexer.blockTokens(source, lexer.tokens);
	} catch {
		return { kind: "uncovered" }; // a lexer bug must never fail a send
	}
	// Tokens ending early are fine: the last slice always runs to the end.
	let offset = 0;
	for (const token of tokens) {
		if (!source.startsWith(token.raw, offset)) return { kind: "uncovered" };
		offset += token.raw.length;
	}
	return { kind: "lexed", lexer, tokens };
}

/** Tables in `token`, counting tables nested in lists and blockquotes. */
function tableCount(token: Token): number {
	const t = token as MarkedToken;
	switch (t.type) {
	case "table":
		return 1;
	case "blockquote":
		return t.tokens.reduce((n, child) => n + tableCount(child), 0);
	case "list":
		return t.items.reduce((n, item) => n + item.tokens.reduce((m, child) => m + tableCount(child), 0), 0);
	default:
		return 0;
	}
}

/**
 * Cells in one raw table row, split exactly the way marked's `splitCells`
 * does but without truncating to the header width: marked drops cells past
 * the header, and a row that has them must not convert (they usually come
 * from Slack syntax such as `<#C1|general>`, whose pipe splits a cell).
 */
function rawCellCount(row: string): number {
	const spaced = row.replace(/\|/g, (_pipe: string, offset: number, str: string) => {
		let escaped = false;
		for (let i = offset - 1; i >= 0 && str[i] === "\\"; i--) escaped = !escaped;
		return escaped ? "|" : " |";
	});
	const cells = spaced.split(/ \|/);
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

// Slack's conversion decodes HTML entities; these are the ones agents write.
// Anything else stays as written.
const NAMED_ENTITIES = new Map(Object.entries({
	amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'", nbsp: "\u00a0",
	AMP: "&", LT: "<", GT: ">", QUOT: "\"",
	mdash: "\u2014", ndash: "\u2013", hellip: "\u2026", middot: "\u00b7", bull: "\u2022",
	rarr: "\u2192", larr: "\u2190", harr: "\u2194", uarr: "\u2191", darr: "\u2193",
	times: "\u00d7", divide: "\u00f7", plusmn: "\u00b1", deg: "\u00b0",
	le: "\u2264", ge: "\u2265", ne: "\u2260",
	lsquo: "\u2018", rsquo: "\u2019", ldquo: "\u201c", rdquo: "\u201d", laquo: "\u00ab", raquo: "\u00bb",
	copy: "\u00a9", reg: "\u00ae", trade: "\u2122",
}));

function decodeEntities(text: string): string {
	return text.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (match, ref: string) => {
		if (ref[0] !== "#") return NAMED_ENTITIES.get(ref) ?? match;
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
 * A run of text source (never code) as rich text elements. Slack syntax is
 * matched on the source before entities are decoded, so `&lt;@U1&gt;` and
 * `&#60;!here&#62;` stay literal text, as Slack's own conversion keeps them.
 * An emoji shortcode only counts between non-alphanumerics, so times such as
 * `10:30:45` and `word:smile:` stay text, and adjacent shortcodes
 * (`:wave::skin-tone-2:`) are each an emoji, as in Slack's conversion.
 */
function textElements(source: string, style: RichTextStyle): RichTextElement[] {
	const out: RichTextElement[] = [];
	let last = 0;
	let lastEmojiEnd = -1;
	const pushText = (s: string) => {
		if (s) out.push(withStyle({ type: "text", text: decodeEntities(s) }, style));
	};
	for (const m of source.matchAll(SLACK_TOKEN)) {
		const start = m.index;
		const end = start + m[0].length;
		let element: RichTextElement | null = null;
		if (m[1]) element = withStyle({ type: "user", user_id: m[1] }, style);
		else if (m[2]) element = withStyle({ type: "channel", channel_id: m[2] }, style);
		else if (m[3]) element = withStyle({ type: "usergroup", usergroup_id: m[3] }, style);
		else if (m[4]) element = withStyle({ type: "broadcast", range: m[4] as BroadcastRange }, style);
		else {
			const name = m[5];
			const before = source[start - 1];
			const boundedBefore = start === 0 || start === lastEmojiEnd || (!isAlnum(before) && before !== ":");
			const plausible = /[a-z]/.test(name) || NUMERIC_EMOJI.has(name);
			if (boundedBefore && !isAlnum(source[end]) && plausible) {
				element = { type: "emoji", name };
				lastEmojiEnd = end;
			}
		}
		if (!element) continue;
		pushText(source.slice(last, start));
		out.push(element);
		last = end;
	}
	pushText(source.slice(last));
	return out;
}

/** Text source still to be matched for Slack syntax, or a finished element. */
type InlineRun =
	| { kind: "source"; source: string; style: RichTextStyle }
	| { kind: "element"; element: RichTextElement };

function sameStyle(a: RichTextStyle, b: RichTextStyle): boolean {
	return a.bold === b.bold && a.italic === b.italic && a.strike === b.strike && a.code === b.code;
}

function inlineRuns(tokens: Token[], style: RichTextStyle, out: InlineRun[]): void {
	for (const token of tokens) {
		const t = token as MarkedToken;
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
			out.push({ kind: "element", element: withStyle({ type: "text", text: t.text }, { ...style, code: true }) });
			break;
		case "link": {
			// One link element per link, styled by what encloses it; a label's
			// own formatting is dropped, as Slack's conversion drops it. An
			// autolink shows its URL, so it carries no label, and its URL is
			// literal; any other destination may hold entities.
			const label = inlineElements(t.tokens).map(displayText).join("");
			const url = t.autolink ? t.href : decodeEntities(t.href);
			const element: RichTextElement = t.autolink
				? { type: "link", url }
				: { type: "link", url, text: label || url };
			out.push({ kind: "element", element: withStyle(element, style) });
			break;
		}
		case "image": {
			// Slack's conversion turns an image into a link to it.
			const url = decodeEntities(t.href);
			const element: RichTextElement = t.text
				? { type: "link", url, text: decodeEntities(t.text) }
				: { type: "link", url };
			out.push({ kind: "element", element: withStyle(element, style) });
			break;
		}
		case "br":
			out.push({ kind: "source", source: "\n", style });
			break;
		case "escape":
			// Slack's conversion still reads `\<!here>` as a broadcast, so an
			// escaped character joins the source around it.
			out.push({ kind: "source", source: t.text, style });
			break;
		case "text":
			if (t.tokens && t.tokens.length > 0) inlineRuns(t.tokens, style, out);
			// The raw source: marked's `text` has numeric entities decoded.
			else out.push({ kind: "source", source: t.raw, style });
			break;
		default:
			// Inline HTML and anything else keep their source, as Slack does.
			out.push({ kind: "source", source: t.raw, style });
		}
	}
}

function inlineElements(tokens: Token[]): RichTextElement[] {
	const runs: InlineRun[] = [];
	inlineRuns(tokens, {}, runs);
	// Escapes and inline HTML arrive as tokens of their own; join neighbouring
	// source so Slack syntax spanning them is still found.
	const merged: InlineRun[] = [];
	for (const run of runs) {
		const prev = merged[merged.length - 1];
		if (run.kind === "source" && prev?.kind === "source" && sameStyle(prev.style, run.style)) {
			merged[merged.length - 1] = { ...prev, source: prev.source + run.source };
		} else {
			merged.push(run);
		}
	}
	return merged.flatMap((run) => (run.kind === "element" ? [run.element] : textElements(run.source, run.style)));
}

function richTextCell(cell: Tokens.TableCell): RichTextCell {
	const elements = inlineElements(cell.tokens);
	// An empty cell holds one space, the same shape Slack's conversion uses.
	if (elements.length === 0) elements.push({ type: "text", text: " " });
	return { type: "rich_text", elements: [{ type: "rich_text_section", elements }] };
}

function tableBlock(table: Tokens.Table): Extract<SlackBlock, { type: "table" }> {
	return {
		type: "table",
		column_settings: table.align.map((align) => (align ? { is_wrapped: true, align } : { is_wrapped: true })),
		rows: [table.header, ...table.rows].map((row) => row.map(richTextCell)),
	};
}

/** What an element shows, as plain text. */
function displayText(e: RichTextElement): string {
	switch (e.type) {
	case "text": return e.text;
	case "link": return e.text ?? e.url;
	case "user": return `<@${e.user_id}>`;
	case "channel": return `<#${e.channel_id}>`;
	case "usergroup": return `<!subteam^${e.usergroup_id}>`;
	case "broadcast": return `<!${e.range}>`;
	case "emoji": return `:${e.name}:`;
	}
}

// Slack parses `text` as mrkdwn, so literal text escapes its control
// characters; only real mentions and broadcasts keep their `<...>` form.
const escapeMrkdwn = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function fallbackText(e: RichTextElement): string {
	if (e.type === "text") return escapeMrkdwn(e.text);
	if (e.type === "link") {
		return e.text && e.text !== e.url ? `${escapeMrkdwn(e.text)} (${escapeMrkdwn(e.url)})` : escapeMrkdwn(e.url);
	}
	return displayText(e);
}

/** A table block as plain rows: header first, cells joined by ` | `. */
function tableAsRows(block: Extract<SlackBlock, { type: "table" }>): string {
	return block.rows
		.map((row) => row.map((cell) => cell.elements[0].elements.map(fallbackText).join("").trim()).join(" | "))
		.join("\n");
}

/** `s` without the whitespace-only lines that separate it from a neighbouring table. */
function trimBlankLines(s: string): string {
	const lines = s.split("\n");
	let start = 0;
	let end = lines.length;
	while (start < end && !lines[start].trim()) start++;
	while (end > start && !lines[end - 1].trim()) end--;
	return lines.slice(start, end).join("\n");
}

/**
 * Decide how one Slack message goes out. Converts when every table in the
 * message is top-level, there are at most `maxTables` of them, and each fits
 * Slack's table limits; the cells of all tables together stay within 10,000
 * characters (Slack's per-message cap). Counts are taken on cell source,
 * which is never shorter than the visible text.
 */
export function convertSlackTables(text: string, maxTables: number): SlackTableConversion {
	const source = text.replace(/\r\n?/g, "\n");
	const lexed = lexBlocks(source);
	if (lexed.kind === "no-table") return { kind: "none" };
	if (lexed.kind === "uncovered") return { kind: "fallback", reason: "the markdown lexer did not map back onto the message" };
	const { lexer, tokens } = lexed;
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
	// Inline-lex only the cells, on the same lexer so reference links resolve.
	try {
		for (const table of topLevel) {
			for (const cell of [...table.header, ...table.rows.flat()]) lexer.inlineTokens(cell.text, cell.tokens);
		}
	} catch {
		return { kind: "fallback", reason: "the markdown lexer failed on a table cell" };
	}

	const blocks: SlackBlock[] = [];
	const fallback: string[] = [];
	const pushProse = (slice: string) => {
		const prose = trimBlankLines(slice);
		if (!prose) return;
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
		const block = tableBlock(token as Tokens.Table);
		blocks.push(block);
		fallback.push(tableAsRows(block));
		proseStart = offset;
	}
	pushProse(source.slice(proseStart));
	return { kind: "blocks", blocks, text: fallback.join("\n\n") };
}

// --- splitting a reply at tables --------------------------------------------

/** Heading, divider, or a short paragraph that is bold from start to end. */
function isLeadToken(token: Token): boolean {
	if (token.type === "heading" || token.type === "hr") return true;
	if (token.type !== "paragraph") return false;
	const text = (token as Tokens.Paragraph).text.trim();
	if (text.length > MAX_LEAD_CHARS || !(text.startsWith("**") || text.startsWith("__"))) return false;
	try {
		const inline = Lexer.lexInline(text, { gfm: true });
		return inline.length === 1 && inline[0].type === "strong";
	} catch {
		return false; // a lexer bug must never fail a send
	}
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
	const source = text.replace(/\r\n?/g, "\n");
	const lexed = lexBlocks(source);
	if (lexed.kind !== "lexed") return [text];
	const { tokens } = lexed;
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
		while (i > prev && !isLeadToken(tokens[i])) i--; // back to the end of the last lead run
		while (i > prev && (tokens[i].type === "space" || isLeadToken(tokens[i]))) {
			if (tokens[i].type !== "space") cut = i;
			i--;
		}
		cuts.push(starts[cut]);
	}
	const bounds = [0, ...cuts, source.length];
	const pieces: string[] = [];
	for (let p = 0; p + 1 < bounds.length; p++) pieces.push(source.slice(bounds[p], bounds[p + 1]).trimEnd());
	return pieces;
}
