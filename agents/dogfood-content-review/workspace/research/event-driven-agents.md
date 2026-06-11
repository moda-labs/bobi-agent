# Research: Event-Driven Agent Architectures

## Summary

Event-driven architectures decouple agent triggers from agent logic,
enabling agents to react to real-world events (webhooks, polls, messages)
without polling or busy-waiting.

## Key Findings

- Webhook-based delivery provides sub-second latency for supported services (GitHub, Slack, Linear)
- Monitor-based polling fills gaps for services without webhook support (email, RSS, custom APIs)
- Topic-based pub/sub routing allows agents to subscribe to specific event streams without filtering noise
- Hybrid approaches (webhooks + monitors) provide the best coverage across service types

## Recommendations

Use webhook adapters for services that support them. Fall back to
monitors with configurable intervals for everything else. Design
event envelopes with a stable schema so agents don't need to know
which delivery path an event took.

## Sources

- modastack event-server implementation (event-server/src/)
- modastack monitor scheduler (modastack/monitors/scheduler.py)
- CloudEvents specification (https://cloudevents.io/)

Last verified: 2026-06-11
