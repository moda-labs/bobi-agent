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
workspace. The manifest is `private: true` and exports TypeScript sources
directly; both consumers — the local Node server (`event-server/src/`) and the
Cloudflare Worker (`event-server/worker/`) — resolve it through the workspace
by package name.

```bash
# from event-server/
npm run build -w core    # compile this package on its own
```

That standalone compile is not a build step anything depends on — nothing
in-repo consumes `dist/`. It exists to prove core still compiles by itself,
which is the property that keeps it a real package rather than a directory:
`tests/test_import_boundaries.py` enforces the same thing from the import
side, in both directions.

**Registry publishing is retired.** `@moda-labs/bobi-events-core@0.1.0` stays
frozen on npm — unpublishing would break unknown consumers — but nothing
produces a new version. The publish path existed solely to carry core across
a repo boundary to the Worker; the Worker is now in this workspace, so the
tarball, its pack/smoke scripts, and the CI step that proved them are all
gone. If a third-party adapter ecosystem ever justifies republishing, that is
a day's work to restore.

Docs: `docs/EVENT_SERVER.md` in the repo covers the architecture, topics, and
security model.
