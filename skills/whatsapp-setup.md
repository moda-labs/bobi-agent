# WhatsApp Setup

Connect a WhatsApp number so people can chat with your agent (#190 Phase 3).
Inbound messages flow from Meta to your **event server** - Meta POSTs webhook
events to `<event-server>/webhooks/whatsapp` - and the agent replies through
the WhatsApp Cloud API.

**Time:** ~15 minutes - Meta app creation and webhook wiring are manual (there
is no manifest shortcut like Slack's).

**Scope:** replies only (reactive). WhatsApp allows free-form messages for 24
hours after a user's last message (the customer-service window). Outside that
window the gateway returns an `outside_message_window` error; agent-initiated
messages require pre-approved template messages, which bobi does not support
yet.

## 1. Create the Meta app and add WhatsApp

1. Open https://developers.facebook.com/apps → **Create App** → type
   **Business**.
2. On the app dashboard, **Add product** → **WhatsApp** → **Set up**. Meta
   provisions a test phone number you can use immediately (production numbers
   are added under WhatsApp → API Setup).
3. Note the **Phone number ID** shown under WhatsApp → API Setup (digits only,
   not the display number). This becomes `WHATSAPP_PHONE_NUMBER_ID` and the
   agent's subscription topic `whatsapp:<phone_number_id>`.

## 2. Create a permanent access token

The API Setup page's temporary token expires in 24 hours - use a System User
token for anything real:

1. Business settings (business.facebook.com/settings) → **Users** → **System
   users** → **Add** (role: Admin).
2. **Add assets** → your app → full control.
3. **Generate new token** → select your app → check the
   `whatsapp_business_messaging` and `whatsapp_business_management`
   permissions → generate, and copy the token (`EAAG…`). This is
   `WHATSAPP_ACCESS_TOKEN`.

## 3. Point the webhook at your event server

1. App dashboard → WhatsApp → **Configuration** → Webhook → **Edit**.
2. **Callback URL**: `<event-server>/webhooks/whatsapp`. A local event server
   is not reachable from Meta - put a public tunnel (cloudflared, ngrok) in
   front of `localhost:8080` and use the tunnel URL.
3. **Verify token**: any string you choose. Set the SAME value on the event
   server (`WHATSAPP_VERIFY_TOKEN` on the Worker, or
   `BOBI_ES_WHATSAPP_VERIFY_TOKEN` for the local server) BEFORE clicking
   Verify - Meta sends a GET handshake the server must answer.
4. Under **Webhook fields**, subscribe to `messages`.
5. Copy the app's **App secret** (App settings → Basic) to the event server
   (`WHATSAPP_APP_SECRET` / `BOBI_ES_WHATSAPP_APP_SECRET`) so inbound events
   are signature-verified. Without it, events are admitted unverified (and
   counted on /health).

## 4. Configure the team

Add the credentials to the runtime `.env`:

```bash
WHATSAPP_ACCESS_TOKEN=EAAG…
WHATSAPP_PHONE_NUMBER_ID=747556541
```

And declare the service in `agent.yaml` so subscription detection picks the
number up:

```yaml
services:
  - name: whatsapp
    events: true
```

At session start the agent registers the number with the event server (a
bubble-signed `POST /whatsapp/numbers` that verifies the token upstream,
stores the send credential, and grants `whatsapp:<phone_number_id>` to this
instance), then subscribes to the number's topic.

## 5. Test it

1. Message the number from any WhatsApp account (the test number requires the
   recipient to be added under API Setup → To).
2. The agent receives a `whatsapp.message` event and replies into the same
   chat via `bobi reply whatsapp:<phone_number_id>:dm:<wa_id> "…"`.

WhatsApp renders its own light formatting (`*bold*`, `_italic_`,
`` ```monospace``` ``), not full markdown - the connector's prompt hint tells
the agent to keep replies short and conversational.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Meta rejects the Callback URL | Verify token mismatch or server unreachable - set `WHATSAPP_VERIFY_TOKEN` on the event server first, and use a public URL |
| Inbound events `401`/ignored | `WHATSAPP_APP_SECRET` on the event server doesn't match the app's App secret |
| Replies fail with `outside_message_window` | The user hasn't messaged within 24h - free-form replies are closed; wait for their next message |
| Replies fail with `no bot token for workspace` | The number isn't registered for this instance - restart the agent so registration runs, and check the token/number id |
| Sends fail with an OAuth error | Token expired (temporary tokens last 24h) - generate a permanent System User token |
| Test number can't message a recipient | Add the recipient under WhatsApp → API Setup → To (test numbers have an allowlist) |
