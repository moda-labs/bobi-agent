"""Shared assets for Bobi's local web UIs."""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
SHARED_ASSET_NAMES = {"tokens.css"}


def resolve_static_asset(local_static_dir: Path, name: str) -> Path | None:
    asset_dir = STATIC_DIR if name in SHARED_ASSET_NAMES else local_static_dir
    target = (asset_dir / name).resolve()
    if not target.is_file() or asset_dir.resolve() not in target.parents:
        return None
    return target
