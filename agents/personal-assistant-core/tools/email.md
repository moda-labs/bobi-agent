# Email

The principal's email, through the `venn` CLI (Gmail by default). See
`tools/venn.md` for the shared mechanics — discovery (`venn tools
search`/`describe`), the `<gmail>` `server_id` from
`workspace/assistant-context.md`, the read-vs-`--confirm` write gate, and
fail-closed. This guide is the **policy**, not the syntax; run `venn docs`
for the commands.

Email is the principal's private inbox — read what a task needs, nothing
more, and never send without confirmation.

## Reading (run directly)

Listing, searching, and reading mail are reads — run them plain (no
`--confirm`). Common needs: the unread/important mail for the briefing, a
specific thread to reply to, "anything from <person> today". Find the right
tool with `venn tools search "list recent emails"` and check its params with
`venn tools describe -s <gmail> -t <tool>` before calling. Headers + snippet
are enough to triage; pull the full body only when you need it to draft.

For the briefing's "needs a reply", apply the importance bar in
`assistant-context.md` — key people and time-sensitive threads surface;
newsletters and promotions don't.

## Sending (draft, then `--confirm`)

Sending is a write — it only goes through with `--confirm`, and per the
role's autonomy line you get the principal's go-ahead first. So:

1. **Draft** the reply in the principal's voice (tone + sign-off from
   `assistant-context.md`), addressing the actual ask and keeping it on the
   right thread.
2. **Show it** to the principal in-thread (`tools/chat.md`) for a yes/no.
3. **Send** only on their confirmation — the `venn tools execute … --confirm`
   call to the send tool (find it via `venn tools search "send email"`).

A draft is not a send; drafting is always safe to do without asking.

## Rules

- **Never send unprompted.** Reads are free; sending crosses the autonomy
  line — confirm first unless the principal told you otherwise.
- **De-dupe by thread.** One thread is one conversation; reply in it, don't
  start a new one.
- **Read narrowly.** Pull the thread the task needs, not the whole inbox.
- **Right inbox.** Target the `<gmail>` `server_id` from the context file,
  not whichever Gmail happens to be default.
