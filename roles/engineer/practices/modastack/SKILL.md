---
name: modastack
version: 1.0.0
description: |
  Modastack CLI tools for communicating with the manager. Use modastack consult
  to ask the manager questions and get blocking responses. Use modastack message
  for fire-and-forget notifications.
---

# Modastack CLI

## Consulting the manager

When you need a decision or guidance from the manager:

```bash
modastack consult "your question"
```

The command blocks until the manager responds and prints the response
to stdout. Use it for:

- Architecture decisions you're unsure about
- Scope questions ("should I also handle X?")
- Priority calls ("should I fix this bug first?")
- Requesting the manager to notify humans on Slack

Do not make assumptions about decisions that should be escalated.
Ask the manager.

## Notifying the manager

For fire-and-forget updates (no response needed):

```bash
modastack message "status update text"
```

Use for progress updates, completion notices, or FYIs where you
don't need a response.
