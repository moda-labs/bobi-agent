"""Shared assets for Bobi's local web UIs."""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
# Assets every local UI must render identically. The brand mark is here for
# the same reason the tokens are: setup and the unified app are one product,
# and a second copy of a logo is a second logo the moment one is edited.
SHARED_ASSET_NAMES = {"tokens.css", "bobi-mark.svg"}


def resolve_static_asset(local_static_dir: Path, name: str) -> Path | None:
    asset_dir = STATIC_DIR if name in SHARED_ASSET_NAMES else local_static_dir
    target = (asset_dir / name).resolve()
    if not target.is_file() or asset_dir.resolve() not in target.parents:
        return None
    return target
