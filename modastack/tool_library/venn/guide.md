# Venn

Interact with external services (email, calendar, CRM, etc.) via the `venn` CLI.
Venn holds OAuth tokens for connected services — you never handle auth directly.
`VENN_API_KEY` comes from `.modastack/.env`.

## List connected services

```bash
venn help list_servers
```

## Find tools for a task

```bash
venn tools search "send an email"
venn tools search "list calendar events"
venn tools search "query salesforce opportunities"
```

## Inspect a tool's schema

```bash
venn tools describe -s <server_id> -t <tool_name>
```

## Execute a tool

```bash
venn tools execute -s <server_id> -t <tool_name> -a '{"param": "value"}'
```

For write operations that require confirmation, add `--confirm`:

```bash
venn tools execute -s <server_id> -t send_email \
  -a '{"to": "user@example.com", "subject": "Hello", "body": "..."}' --confirm
```

## Tips

- Use `venn tools search` first to discover the right `server_id` and `tool_name`.
- Server IDs are instance-specific (e.g., `work-gmail` vs `personal-gmail`).
- The `--raw` flag on the root command outputs JSON instead of formatted tables.
