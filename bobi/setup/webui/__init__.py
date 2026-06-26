"""Local web UI for `bobi setup` — a foreground FastAPI server on 127.0.0.1.

The wizard drives a mode-aware stage machine; the LLM serves the digestion
brain (conversation → spec) and the Build pour (spec → pack files). See
`server.py` for the app and the socket→uvicorn launcher.
"""
