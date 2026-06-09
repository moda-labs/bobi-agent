# Venn

Interact with external services (email, calendar, CRM, etc.) via the `venn` CLI.
Venn holds OAuth tokens for connected services — you never handle auth directly.

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
venn tools execute -s <server_id> -t <tool_name> -a '{"to": "user@example.com", "subject": "Hello"}' --confirm
```

## Common patterns

### Email

```bash
venn tools search "list emails"
venn tools execute -s work-gmail -t list_messages -a '{"maxResults": 10, "q": "is:unread"}'
venn tools execute -s work-gmail -t send_email -a '{"to": "...", "subject": "...", "body": "..."}' --confirm
```

### Calendar

```bash
venn tools search "list calendar events"
venn tools execute -s personal-google-calendar -t list_events -a '{"maxResults": 5}'
```

### CRM

```bash
venn tools search "salesforce opportunities"
venn tools execute -s salesforce -t query_records -a '{"object": "Opportunity", "limit": 10}'
```

## Tips

- Use `venn tools search` first to discover the right server_id and tool_name.
- Server IDs are instance-specific (e.g., `work-gmail` vs `personal-gmail`).
- The `--raw` flag on the root command outputs JSON instead of formatted tables.
