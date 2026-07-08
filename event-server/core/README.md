# @moda-labs/bobi-events-core

Runtime-agnostic core of the [Bobi](https://github.com/moda-labs/bobi-agent)
event protocol: normalized events, the webhook ingest pipeline, channel
adapters (Slack, WhatsApp, GitHub, Linear), conversation references, and the
delivery circuit breaker. Consumed by both the local Node event server
(bundled with Bobi) and Cloudflare Worker deployments.

## Entry points

- `@moda-labs/bobi-events-core` - normalized event model, topic keys, webhook
  pipeline, signatures.
- `./channels` - outbound channel adapters and send helpers.
- `./conversation` - channel-agnostic conversation reference codec.
- `./circuit-breaker` - delivery loop detection.
- `./adapters/chat-sdk-slack` - Slack Chat SDK webhook bridge.

## Development

This package lives in the `bobi-agent` repo as the `event-server/core/`
workspace. The workspace manifest is `private: true` and exports TypeScript
sources directly; do not publish it as-is. The publishable tarball (compiled
ESM + `.d.ts`, exports pointing at `dist/`) is produced by:

```bash
# from event-server/
npm run pack:publish -w core   # build dist/ + npm pack, prints tarball path
npm run smoke -w core          # pack + install into a scratch consumer + verify
```

Docs: `docs/EVENT_SERVER.md` in the repo covers the architecture, topics, and
security model.
