# Gmail (support inbox)

Read the support inbox through the **Venn MCP**. Email is not a native
modastack event source, so the `email-watch` monitor polls on an interval:
its check agent reads new support mail through Venn and emits
`monitor/support.email` for messages that read like a bug report.

The inbox/label and the bug-vs-not guidance come from
`workspace/support-context.md`.

## Reaching Venn

Venn's tools are deferred — load them before calling:

```
ToolSearch "select:mcp__claude_ai_Venn__generate_session_id,mcp__claude_ai_Venn__search_tools,mcp__claude_ai_Venn__execute_tool"
```

At the start of a run call `generate_session_id` once and pass `session_id`
+ a stable `user_intent` ("Triage support email for bugs") on every
subsequent Venn call.

**Use the right inbox.** If several Gmail instances are connected via Venn,
target the support-inbox instance named in `workspace/support-context.md` (its
Venn `server_id`) — not whichever Gmail happens to be default. Below,
`<gmail>` is that `server_id` and `<support-address>` is the inbox address
from the context file.

**Find new support mail** — `execute_tool` with `<gmail>` / `list_emails`.
Returns lightweight `{id, threadId}` stubs:

```
execute_tool(server_id="<gmail>", tool_name="list_emails",
  tool_args={"userId":"me","q":"to:<support-address> newer_than:1d","maxResults":25})
```

**Read a message** — `<gmail>` / `find_email` by `id`. For triage,
headers + snippet are enough and cheaper than the full body:

```
execute_tool(server_id="<gmail>", tool_name="find_email",
  tool_args={"userId":"me","id":"<message id>","format":"metadata",
             "metadataHeaders":["From","To","Subject","Date"]})
```

Use `format:"full"` only when you need the body to judge a borderline case;
the body parts are base64url-encoded under `payload.parts`, the preview is
in `snippet` (plain text). Headers (From/Subject/Date) are in
`payload.headers`.

## What to pass into triage

For each candidate, hand `triage-issue` the sender, subject, a body
summary, whether more than one person reported the same thing, and the
Gmail message link/permalink (for the ticket and the log).

## Filtering: two steps (see support-context.md for the full version)

**Step 1 — is it a real customer support email?** This is the gate; drop
everything else and do not emit a signal for it:
- **Vendor / sales / marketing outreach** — cold pitches, partnership/SEO
  offers from company domains (e.g. a "checking back about <your product>"
  growth pitch from a vendor domain). DROP.
- **Automated / no-reply notifications** — receipts, product promos,
  security notices from `*-noreply@`/`notifications@` (e.g. a
  `*-noreply@google.com` product announcement). DROP.
- Keep only **first-person mail from an individual** describing their own
  experience with the product.

**Step 2 — bug vs not-a-bug** (only for mail that passed Step 1). Real =
something is broken for the user (chat stuck, message never arrived, audio
not transcribed, story/translation missing, can't sign up/onboard, or
**paid/charged but locked out**). Not-a-bug = how-to / language-support
questions, feature requests, translation-quality opinions, and pure
billing/refund *questions*. Pass real-customer borderline cases to triage
too (they get a `not_real` verdict with the category, so the daily report
accounts for them). Note: "paid but no access" is a **real** issue, not a
billing question — a charged customer who can't use the product is a broken
entitlement flow, not a billing query.

## Key rules

- **Connector dependency.** Venn is a claude.ai connector, reached through
  the account login (not `.mcp.json`). A `modastack agents launch
  --non-interactive` check agent (the same path the `email-watch` monitor
  uses) can load Venn and read the inbox, so the autonomous path works on a
  host where this account's connectors are available. The residual risk is
  auth freshness — if the Venn↔Gmail OAuth expires, the check agent gets
  nothing. On any unreachable/empty-tools case, emit nothing and note the
  gap rather than guessing; PostHog (direct API) is the always-on backstop.
- **Do not reply to the customer.** This pack triages and files; it does
  not send email. Any customer-facing reply is a human's call.
- **De-dupe by thread.** One email thread is one signal — do not emit a
  new signal for each reply in the same thread.
