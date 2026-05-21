"""Event-driven architecture for modabot.

Events flow: producers → bus → consumer (manager)

Producers push events from external sources (webhooks, pollers, tmux).
The bus queues them. The manager consumes batches and reasons about them.

Standard event format:
{
    "type": "ticket.created",       # dotted event type
    "source": "linear",             # which system produced it
    "timestamp": "2026-05-20T...",  # when it happened
    "data": { ... }                 # source-specific payload
}
"""
