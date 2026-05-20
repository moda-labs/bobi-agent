"""Input channels for the manager.

Each channel module must implement:
    async def gather(config: dict) -> list[dict]
        Returns events/items for the manager's context.

    def hash_key(items: list[dict]) -> str
        Returns a string that changes when the channel has new data.
        Used by the watcher for cheap change detection.

    def format_context(items: list[dict]) -> str
        Formats items into readable text for the manager prompt.
"""
