# 1123 - Send Slack Markdown tables as Block Kit tables that wrap

Status: **approved at Gate 1 by Zach on 2026-10-19**, with the recommended answer to both questions ([comment](https://github.com/moda-labs/bobi-agent/pull/1128#issuecomment-6273694587)); D11 and D12 record them.
Issue: [#1123](https://github.com/moda-labs/bobi-agent/issues/1123), filed by Luke on 2026-10-13.
Base: `main` @ `6ed28a54`. Code citations are `path:line` at that commit.
Slack behaviour verified 2026-10-16 against docs.slack.dev and the live Slack API (§3).
Reviewed for engineering, design and scope before publishing; their findings are folded in.

## 1. Summary

A reply whose Markdown holds one GFM table that fits Slack's table limits goes out as Block Kit blocks instead of `markdown_text`.
The blocks are a `markdown` block for the text before the table, a `table` block with wrapped columns, and a `markdown` block for the text after it.
Every other message goes out exactly as it does today.

Slack allows one table per message.
So a reply with several tables goes out as several messages, in source order, one table each.
That is the behaviour change readers will notice besides wrapping (D11).

The message's fallback `text` carries the prose plus the table as plain rows.
That makes `bobi read-conversation` show table rows that it omits today.

If Slack rejects the blocks anyway, the adapter resends the same message as `markdown_text`, exactly today's payload, and logs the rejection.
The worst case is therefore today's behaviour plus one extra API call.

The change lives in the Slack adapter and the shared chunker.
It adds one dependency, the `marked` Markdown parser, which has no dependencies of its own, so the wheel's dependency audit gains one entry.
There is no `core.ts` logic change and no change to the `/channels/send` wire format.

Gate 1 settled both open questions: multi-table replies split into one message per table (D11), and the live checks post test messages as the eng-team bot in C0BAEN48KQR (D12).

## 2. Root cause, confirmed

The gateway sends every Slack reply as raw Markdown in `markdown_text` (`event-server/core/src/channels.ts:300`).
Slack converts that Markdown into blocks on its side.
Five posts by the eng-team bot in its status channel show what that conversion produces for a table (read with `conversations.history`, 2026-10-16):

- The blocks come out as `rich_text`, `table`, `rich_text` in source order, with the table inline rather than appended.
- Every table cell is a `rich_text` cell.
- The table block has **no `column_settings`**, so `is_wrapped` takes its default of `false`.
- The message `text` Slack generates leaves out the table rows.

No wrapping and no table rows in readback are both consequences of letting Slack build the table.
`markdown_text` has no parameter to change either one.

## 3. Slack facts this design rests on

| Code | Fact | Source |
|---|---|---|
| F1 | `markdown_text` "should not be used in conjunction with blocks or text"; the combination fails with `markdown_text_conflict`. The Chat SDK refuses it before sending. | [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/), [chat.update](https://docs.slack.dev/reference/methods/chat.update/), `@chat-adapter/slack@4.30.0` `dist/api.js` |
| F2 | A table block holds at most 100 rows, 20 cells per row and 20 `column_settings`. `is_wrapped` "defaults to `false`". `align` is `left`, `center` or `right`. Cells are `rich_text`, `raw_text` or `raw_number`. | [table block](https://docs.slack.dev/reference/block-kit/blocks/table-block/) |
| F3 | "A single table's character count across all cells cannot exceed 10,000 characters", and the same cap applies to all table cells in one message. The issue does not list this limit. | [table block](https://docs.slack.dev/reference/block-kit/blocks/table-block/) |
| F4 | One `table` block per message. Slack's own Block Kit skill says "Table: only one `table` block per message" and lists two tables in one message as invalid. A developer recorded the real error as `invalid_blocks` with `errors: ["only_one_table_allowed"]`, including when both tables sat inside a `markdown` block. The Chat SDK caps itself at one native table. docs.slack.dev's table page does not state the limit, and no evidence here shows whether `markdown_text` with two tables is rejected too. | [slackapi/slack-skills-plugin](https://github.com/slackapi/slack-skills-plugin/blob/main/skills/block-kit/SKILL.md) @ `ad6be880` (2026-10-15) and its `references/design-principles.md`; [developer note](https://github.com/nutslove/Knowledges) (2026-03-13); SDK `convertTableToBlocks` |
| F5 | The `markdown` block renders standard Markdown, including tables. All markdown blocks in one payload share a 12,000-character cap, and one block may become several after Slack translates it. | [markdown block](https://docs.slack.dev/reference/block-kit/blocks/markdown-block/) |
| F6 | When a message has blocks, `text` becomes the notification fallback, and screen readers read `text`, not the blocks. | [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/), accessibility section |
| F7 | `blocks.validate` (no scopes, posts nothing) accepted this design's exact layout: `markdown`, then `table` with wrapped `rich_text` cells holding a link, bold, italic, strike, code, a user mention and an emoji, then `markdown`. It rejected 21 cells, 101 rows, 21 `column_settings`, an empty `raw_text` cell, and a `null` `column_settings` entry, although the docs say `null` skips a column. It accepted two tables and 12,000 cell characters, so it does not enforce F3 or F4. | [blocks.validate](https://docs.slack.dev/reference/methods/blocks.validate/), run 2026-10-16 with the eng-team bot token |
| F8 | A newer `data_table` block exists. It paginates (5 rows per page by default), requires a caption, forbids `rich_text` header cells, and documents no wrapping. | [data table block](https://docs.slack.dev/reference/block-kit/blocks/data-table-block/) |

## 4. Design

### 4.1 Where the code lives

Conversion belongs to the Slack adapter.
Splitting a reply into messages already belongs to `chunkForChannel` (`event-server/core/src/channels.ts:223`), which enforces each channel's declared length budget.
Slack's one-table rule is the same kind of declared limit, so it becomes a capability enforced by the same chunker.

- `ChannelCapabilities` (`event-server/core/src/channels.ts:77`) gains `maxTablesPerMessage?: number`.
  Slack declares `1`; WhatsApp and Discord declare nothing and stay byte-identical.
- `chunkForChannel` splits on table boundaries when that capability is set (§4.6), then applies the existing length budget.
- `slackContent` (`event-server/core/src/channels.ts:300`) chooses, per message, between today's `{markdownText}` and the new `{blocks, text}` (§4.3).
- The table logic (lex, locate, convert) goes in a new module, `event-server/core/src/markdown-tables.ts`, exported through the core package's `exports` map so tests import it the way they import `channels`.

`handleChannelsSend` (`event-server/core/src/core.ts:2202`) keeps its loop unchanged.
That loop already posts multi-message replies: the first message carries identity (a placeholder edit and the returned `ts`), follow-ups are posted 300 ms apart, and a failed follow-up returns the "partially delivered, do not resend" error (`event-server/core/src/core.ts:2346`).
Table splits ride that loop, so one code path still delivers every multi-message reply.
The same core runs in the Cloudflare Worker and in the local Node server, so both get the change.

### 4.2 Finding tables

Tables are found with a real GFM parser, never with regexes.
The parser is [`marked`](https://www.npmjs.com/package/marked) (MIT, no dependencies, D7).
Its lexer returns block tokens with their exact source (`raw`), table alignment, and inline tokens for every cell, and it ignores pipe tables inside code fences.
Line endings are normalised to `\n` before lexing.
The lexer only runs when some line looks like a GFM delimiter row (`|---|`), so replies without a table cost nothing extra.

A **top-level** table is a block token directly under the document.
A table nested in a list item or blockquote is never converted, but it still counts toward the one-table cap.

### 4.3 Converting one message

A message converts only when all of these hold:

1. It contains exactly one table, counting nested tables.
2. That table is top-level.
3. The table has at most 100 rows, header included (F2).
4. The table has at most 20 columns (F2).
5. No body row has more cells than the header (§4.4).
6. The cells' Markdown source totals at most 10,000 UTF-16 units (F3, R3).

Otherwise the message goes out as `markdown_text`, byte-identical to today, and the adapter logs which condition failed.
An over-limit table therefore still renders as it does today, as a native table that does not wrap.

A converted message is posted with:

- `blocks`: a `markdown` block with the source before the table, then the `table` block, then a `markdown` block with the source after it.
  A block is omitted when its text is blank.
  Before and after are exact source slices, so the prose goes through Slack's same Markdown renderer.
  That it looks identical to today is an inference, which the live check confirms (§7 step 5).
- `text`: the fallback described in §4.7.
- No `markdown_text` (F1) and no `block_id` (the table page asks for a fresh `block_id` on every update, and leaving it out lets Slack generate one).

The table block carries one `column_settings` object per column: `{"is_wrapped": true}`, plus `align` when the GFM delimiter row sets one (`:-:` gives `center`, `-:` gives `right`).
Every column gets an object, because Slack rejects `null` entries (F7).
Every column wraps, because GFM columns have no type and every cell is inline text, so the issue's "text columns" means all of them (D3).
If the live check shows short columns shrinking awkwardly, on desktop or mobile, wrapping narrows to columns that have a cell over 20 characters.

### 4.4 Cells

Every cell becomes a `rich_text` cell with one `rich_text_section`, the same shape Slack's own conversion produces (§2).
The header row stays `rows[0]` with no added styling, which also matches Slack's conversion.

| Markdown in a cell | `rich_text` element |
|---|---|
| plain text | `text` |
| `**bold**`, `__bold__` | `text` with `style.bold` |
| `*italic*`, `_italic_` | `style.italic` |
| `~~strike~~` | `style.strike` |
| `` `code` `` | `style.code` |
| `[label](url)`, reference links, `<url>`, a bare URL | `link` with `url`, `text`, and the styles of anything nested inside |
| `<@U…>`, `<#C…>`, `<!subteam^S…>` | `user`, `channel`, `usergroup`, found in text and inline-HTML tokens alike |
| `<!here>`, `<!channel>`, `<!everyone>` | `broadcast`. Whether today's `markdown_text` pings for these is checked live, and the mapping follows what today does |
| `:shortcode:` | `emoji`, matched conservatively so times such as `10:30:45` stay text |
| images, other inline HTML, footnote references | their plain source text |
| an empty cell | a `rich_text` cell with an empty section (accepted by `blocks.validate`; an empty `raw_text` is rejected, F7) |

Nested styles combine, so `**[PR](url)**` becomes a bold link.
Short rows are padded with empty cells.
A row with more cells than the header blocks conversion instead of dropping cells.
GFM would hide the extra cells, and they usually come from Slack syntax such as `<#C1|general>` or `<url|label>`, whose `|` splits a cell.
Converting there would lose content without any error from Slack.

### 4.5 Limits and fallback

| Limit | Source | Over the limit |
|---|---|---|
| 100 rows, header included | F2 | that message goes out as `markdown_text` |
| 20 columns, so at most 20 `column_settings` | F2 | `markdown_text` |
| 10,000 cell characters | F3 | `markdown_text` |
| a body row wider than the header | §4.4 | `markdown_text` |
| one table per message | F4 | the reply is split (§4.6) |
| 12,000 characters per message | existing budget (`event-server/core/src/channels.ts:319`) | the existing chunker splits as today |

The 12,000-character budget also covers F5's markdown-block cap, because the prose blocks are part of the message.

### 4.6 One table per message: splitting

When a reply holds two or more table-bearing top-level tokens, `chunkForChannel` cuts it into pieces holding one each, before applying the length budget.
A table-bearing token is a table, or a list or blockquote with a table inside it.

**The cut.** Between two consecutive table-bearing tokens, find the last unbroken run of lead tokens.
A lead token is a heading, a horizontal rule, or a paragraph that is bold from start to end.
The cut falls just before the first token of that run.
With no lead token between them, the cut falls immediately before the second table.
Each piece keeps its source bytes verbatim, and the pieces post in source order.

This keeps a section's lead-in with its table.
The roadmap-pm standup writes each project section as a divider, a bold project heading, a one-sentence takeaway, then a table.
The rule cuts before the divider, so each message opens with its divider and heading, and the previous section's "Next steps" bullets stay with the table above them.
A paragraph that only starts bold, such as `**Note:** ...`, is not a lead token, so commentary stays with the table it describes.

**Edges.**

- A reply with at most one table is not cut; within budget it still comes back as one byte-identical chunk.
- Two tables with nothing between them are cut immediately before the second.
- A single token holding two tables (a blockquote, say) cannot be cut, so that message goes out as `markdown_text`.
- The length chunker can still cut a piece longer than 12,000 characters inside its table.
  The half holding the header and delimiter row still parses as a table and converts; the other half is plain text, as it is today.

**The 8-message cap.** `MAX_CHUNKS` (`event-server/core/src/channels.ts:144`) caps the whole reply, counted across all pieces.
Pieces that would land past message 7 join message 8, which goes out as `markdown_text`.
Message 8 is truncated only if it exceeds 12,000 characters, which is today's runaway rule.
So a 9-table reply sends 7 wrapped tables and one message carrying the last 2 tables as `markdown_text`.

**What readers see.** A conversation without a thread anchor posts every piece as its own top-level message, each starting its own thread.
That is how over-budget replies already behave (`event-server/core/src/channels.ts:332`).
In a thread every piece lands in the same thread, and in a DM they arrive as consecutive messages.
Pieces are posted 300 ms apart, which Slack's roughly one-post-per-second channel limit absorbs as a burst only for a few pieces (R6).

### 4.7 Readback and the fallback text

`bobi read-conversation` prints each message's `text` (`bobi/cli.py:1518`), which the adapter reads from Slack unchanged (`event-server/core/src/channels.ts:280`).
A converted message's `text` is the prose verbatim, with the table written as plain rows: the header row first, one line per row, cells joined by ` | `, no delimiter row, and links as `label (url)`.

- Readback now includes the table rows for every table this change wraps.
  The issue only asks that readback not get worse.
- A message that falls back to `markdown_text` reads back exactly as today, without the table rows.
- Notifications and screen readers get the content instead of a message missing its table (F6), without the delimiter row and Markdown link syntax that raw GFM would read aloud.

No `bobi read-conversation` code changes.

### 4.8 Edits, streaming updates and files

- **Placeholder edit** (`mode: final` with `edit_ref`): `chat.update` receives the same `blocks` and `text` a post would.
  The existing loop posts the remaining pieces as follow-ups.
  Running `bobi reply --edit` again on a split reply rewrites message 1 and posts the later pieces again, the same as an over-budget reply does today.
- **Streaming update** (`mode: update`): this path rewrites one message and never chunks (`event-server/core/src/core.ts:2339`).
  It converts when the text holds exactly one convertible table, and otherwise sends `markdown_text` as today.
  No caller in this repo sends `mode: update`; `bobi reply` always sends `final`.
- **Files with a placeholder** (`--file` with `--edit`): the placeholder edit goes through `adapter.update` (`event-server/core/src/core.ts:2307`), so its text converts like any edit.
- **File comments without a placeholder** (`event-server/core/src/core.ts:2293`) are unchanged.

### 4.9 Safety net for rejected blocks

If `chat.postMessage` or `chat.update` rejects a converted message with `invalid_blocks`, `invalid_blocks_format` or `msg_blocks_too_long`, the adapter resends that one message once as `markdown_text`.
Those errors mean Slack rejected the payload before posting, so the resend cannot duplicate a message.
Any other error surfaces exactly as today and is never resent.
That includes `ratelimited`, `channel_not_found`, and `internal_error` or `fatal_error`, which can follow a partly completed post.

The adapter logs the rejection with `console.warn`, as `core.ts` already does for authorization failures.
The log carries Slack's `error`, `response.errors` and `response.response_metadata.messages` (the Chat SDK's `SlackApiError` exposes the response), and never cell text.
A resent follow-up piece that succeeds does not trigger the partial-delivery error.

The net bounds every risk Slack reports as a block error: an undocumented limit, a disagreement between `marked` and Slack's Markdown translator (R2), or a cell element Slack's schema refuses.
It does not cover a table that is accepted but looks wrong; the live check covers that.

### 4.10 Where agents learn the behaviour

Agents learn Slack formatting from `skills/bobi.md` and their team's own skills.
The descriptor's `promptHint` (`event-server/core/src/channels.ts:327`) is never read by anything, so this spec leaves it alone.
`skills/bobi.md` gains a short note next to `bobi reply` (`skills/bobi.md:139`):

- tables are sent as native Slack tables that wrap long cells;
- each extra table in one reply arrives as its own message, so prefer one table per reply and give each extra table its own heading;
- keep tables to about five columns;
- tables inside lists or quotes do not wrap.

`docs/EVENT_SERVER.md:211` and the module comment at the top of `channels.ts` change from "Slack: native `markdown_text`" to describe the table path.
moda-agents' standup skill still tells agents "Slack does not wrap table columns". That line belongs to the moda-agents repo and changes after rollout (§7 step 7).

### 4.11 Decisions

| Code | Decision | Why |
|---|---|---|
| D1 | Use `table`, not `data_table`. | The issue specifies `table` with `is_wrapped`. `data_table` paginates at 5 rows, needs a caption, drops header formatting and documents no wrapping (F8). Slack's own guide maps "a short set of rows with the same columns" to `table`. |
| D2 | At most one table per message on every path that can split. | F4 is a hard limit for blocks. The paths that cannot split keep today's `markdown_text`: a streaming update, a single token holding two tables, and message 8 under the cap. |
| D3 | Wrap every column; take `align` from GFM. | Every GFM column is text. §4.3 names the narrower rule the live check can switch to. |
| D4 | Cells are always `rich_text`. | It preserves formatting and matches Slack's own conversion (§2). |
| D5 | The fallback `text` is the prose verbatim plus the table as plain rows. | Fuller readback, notifications and screen-reader text (§4.7). |
| D6 | Resend once as `markdown_text` on block errors only. | No regression past today's behaviour, and no double posts (§4.9). |
| D7 | Parser: `marked`, added with `npm install -w core marked`. | A single package with no dependencies, so the wheel's bundled-dependency audit (`bobi/events/artifact.py:98`, two entries today) gains one entry. The micromark stack behind remark pulls in about 49 packages, each needing an audit entry and verbatim license text. |
| D8 | Splitting is a chunker capability; conversion is adapter-local. | One chunker and one send loop, with the limit declared next to Slack's other limits. |
| D9 | No feature flag. | D6 covers rejections, and a bad render rolls back with the previous release and Worker version. |
| D10 | The cut rule in §4.6. | Cutting immediately before each table would strand a section's heading and takeaway at the bottom of the previous message. |
| D11 | A reply with several tables becomes one message per table, in source order (§4.6). | Every table wraps, including the multi-table roadmap-pm standup. A top-level standup with four tables becomes four top-level posts, so four notifications, four threads and one returned `ts`. Keeping one `markdown_text` message would have left those tables unwrapped. |
| D12 | The live checks (§7 step 5) post test messages as the eng-team bot in its status channel, C0BAEN48KQR. | That bot is the only Slack credential available here, and there is no `SLACK_TEST_CHANNEL` or dev-workspace soak config. |

## 5. Issue requirements, and where this spec settles them

| Issue requirement | Settled in |
|---|---|
| Convert a GFM table to a Block Kit `table` with `is_wrapped: true` on its text columns | §4.3, D3 |
| Leave text outside the table as it is today | §4.3 (exact source slices in `markdown` blocks) |
| 100 rows, 20 cells per row, 20 `column_settings`, falling back to today's `markdown_text` table | §4.5, plus the 10,000-character limit the issue does not list (F3) |
| Links and bold survive in `rich_text` cells | §4.4 |
| Message order matches the source: text, then table, then text | §4.3 block order, §4.6 piece order, §2 (Slack keeps the table inline) |
| `bobi read-conversation` readback does not get worse | §4.7 (better for converted tables, unchanged otherwise) |
| Verify Slack's real behaviour against docs.slack.dev, with citations | §3 |
| Done when a real post from a deployed team wraps after a `BOBI_VERSION` roll | §7 step 7 |

## 6. Scope

**In:**

- Slack sends: posts, finals, placeholder edits, and streaming updates.
- Table detection and conversion, limits and fallback, multi-table splitting, the fallback text, and the safety net.
- The `marked` dependency, plus its entry in the wheel's bundled-dependency audit (`bobi/events/artifact.py:98`), which also generates `THIRD_PARTY_NOTICES.txt`.
  If `marked`'s license file is not named `LICENSE`, the audit's license lookup also accepts `LICENSE.md`.
- The `skills/bobi.md` note and the docs in §4.10.
- Tests at every level in §7.

**Out:**

- File comments without a placeholder keep today's behaviour.
- Readback for messages that fall back to `markdown_text` is unchanged.
  Rendering Slack-built table blocks into readback text is possible later, but it is not needed for the issue's "not worse" bar.
- WhatsApp and Discord do not render tables and are untouched.
- `data_table`, per-team flags, and changes to the 12,000-character budget.

## 7. Verification plan

1. **Unit (vitest).**
   - The converter: block order, wrapping, alignment, every cell mapping in §4.4, empty, short and over-wide rows, escaped pipes, and CRLF input.
   - Each limit at its exact boundary: 100 vs 101 rows, 20 vs 21 columns, 10,000 vs 10,001 cell characters.
   - Non-conversion: no table, a table in a code fence, a nested table, two tables in one message.
   - The cut rule on the real standup shape, and its edge cases.
   - Chunker invariants: byte-identical within budget with at most one table, content preserved in order, and the cap at 9 and 10 tables.
   - Discord and WhatsApp stay byte-identical on multi-table input.
2. **Handler (core.spec.ts, `stubSlackApi`).** These tests assert the exact Slack payloads:
   - one table gives one `chat.postMessage` with `blocks` and `text` and no `markdown_text`;
   - two tables give two posts in order, with the first `ts` returned;
   - a placeholder edit and a `--file --edit` placeholder send `chat.update` with blocks;
   - `mode: update` with one table converts, and with two it does not;
   - an over-limit table gives `markdown_text`;
   - `invalid_blocks` on a post, an update and a follow-up piece triggers exactly one `markdown_text` resend, with no partial-delivery error;
   - `ratelimited`, `internal_error` and `fatal_error` are never resent.
3. **End-to-end through the CLI (tests/integration/test_channel_gateway.py).** `bobi reply` runs through the real local event server to the Slack stub (`tests/integration/test_channel_gateway.py:34`).
   A table reply arrives as blocks with the §4.7 `text`, and a two-table reply arrives as two posts in order.
   Readback is proven by the live step, because the stub serves a fixed thread.
4. **Packaging.** `tests/integration/test_packaged_event_server.py` builds the wheel with the new dependency and passes the audit.
5. **Live check, posting to Slack (D12).**
   - `blocks.validate` on generated payloads runs once as proof on the PR, and the same check joins the gated live suite (`tests/integration/test_slack_live.py:155`).
     No CI job carries a Slack token, so a separate gated test would only ever skip.
   - Post one-table and two-table replies.
   - Post a two-table `markdown_text` message, which settles F4 for today's path.
   - Post one prose reply both ways and diff the stored blocks, which also shows link unfurls (R4).
   - Read back with `bobi read-conversation`.
   - A person checks desktop, mobile and VoiceOver: the cells wrap, the table sits between its text, and emoji, mentions and broadcasts behave as they do today.
6. **Proof on the PR.** Test output, the `blocks.validate` run, and screenshots of the live thread.
7. **The issue's done-when, after merge.** The fleet Worker serves Slack sends, and moda-agents `release.yml` deploys it during the release train (`docs/RELEASE_RUNBOOK.md`).
   After the release, the Worker deploy and a `BOBI_VERSION` roll, the eng-team bot (itself a deployed team) posts a table reply.
   Reading it back with `conversations.history` must show a `table` block with `is_wrapped: true`, and a person confirms it looks right.
   Then Luke is told, so the moda-agents standup line from §4.10 can change.

## 8. Implementation plan (tests first)

1. `cd event-server && npm install -w core marked`, then add the `marked` audit entry in `bobi/events/artifact.py`.
2. Write the failing tests from §7 steps 1-4.
3. Add `markdown-tables.ts`: lex, locate tables, check limits, convert cells, build the fallback text.
4. Add the `maxTablesPerMessage` capability, the table pre-split and the cross-piece cap in `chunkForChannel`.
5. Return `{blocks, text}` from `slackContent` when converting, and add the §4.9 resend and logging to `send` and `update`.
6. Update `skills/bobi.md`, `docs/EVENT_SERVER.md` and the `channels.ts` module comment.
7. Run the vitest and Python suites, then the live checks in C0BAEN48KQR (D12).

## 9. Risks

| Code | Risk | Mitigation |
|---|---|---|
| R1 | How it looks cannot be proven without a real post: wrap widths on desktop and mobile, the table sitting between its markdown blocks, empty cells, emoji and mentions. Third-party guides claim tables always render at the bottom of a message, which contradicts what Slack stores for its own conversion (§2). | Live check (§7 step 5, D12) before merge. |
| R2 | `marked` and Slack's Markdown translator may disagree about what counts as a table. | A disagreement can surface as `only_one_table_allowed`, which the safety net resends as `markdown_text` (§4.9). A row-width mismatch is refused before sending (§4.4). |
| R3 | Slack does not say how it counts the 10,000 characters. | We count cell source, which is never less than the visible text, so a link-heavy table falls back slightly early instead of being rejected. |
| R4 | Link unfurls may differ between `markdown_text` and blocks messages. | The live diff in §7 step 5; only messages with a table are affected. |
| R5 | Bundle growth in the Worker and in the wheel's local server. | One dependency-free package; the PR reports the measured size. |
| R6 | Splitting adds messages and notifications, which Slack calls a last resort, and posts them 300 ms apart against a channel limit of about one per second. | Only replies with two or more tables split, and the 8-message cap bounds a burst (D11). |
| R7 | A reference link whose definition sits in another piece does not resolve after a split. | Rare in agent output; the link then renders as its literal text. |

## 10. Gate 1 decisions

Zach approved the spec on 2026-10-19 and took the recommended answer to both open questions.

**Q1. Should multi-table replies become one message per table, so every table wraps?** Yes (D11).
Such a reply splits into one message per table, in source order, with the cut rule in §4.6.
Whether today's single-message standup is accepted by Slack at all was unverified (F4); the live check settles it.

**Q2. OK to post test messages as the eng-team bot in its status channel, C0BAEN48KQR?** Yes (D12).
The live checks in §7 step 5 post there.
