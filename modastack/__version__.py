from pathlib import Path

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"

__version__ = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
