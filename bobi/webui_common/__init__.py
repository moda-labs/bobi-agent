"""Shared assets for Bobi's local web UIs."""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
SHARED_ASSET_NAMES = {"tokens.css", "fonts.css", "chrome.css"}
# The vendored brand faces both UIs load via fonts.css. Shared as a prefix
# rather than by name so adding a subset needs no code change.
SHARED_ASSET_PREFIXES = ("fonts/",)


def _is_shared(name: str) -> bool:
    return name in SHARED_ASSET_NAMES or name.startswith(SHARED_ASSET_PREFIXES)


def resolve_static_asset(local_static_dir: Path, name: str) -> Path | None:
    asset_dir = STATIC_DIR if _is_shared(name) else local_static_dir
    target = (asset_dir / name).resolve()
    if not target.is_file() or asset_dir.resolve() not in target.parents:
        return None
    return target
