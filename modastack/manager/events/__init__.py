"""Event-driven architecture for modabot.

Events arrive from the centralized event server (Cloudflare Worker)
via WebSocket. Slack messages arrive via Socket Mode. Both inject
directly into the manager session.
"""
