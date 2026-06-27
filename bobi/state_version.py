"""State format version marker (<run>/state/format_version).

Volumes outlive images — when the on-disk state format changes, a newer
bobi must know whether it can read the persisted state and an older
bobi must refuse to corrupt a format it doesn't understand.

The version is a simple integer written as plain text. Semantics:

- **Equal:** proceed normally.
- **Older (on-disk < code):** a future migration hook will upgrade;
  today, the code writes the current version (first run or upgrade
  from pre-versioned state).
- **Newer (on-disk > code):** refuse to start. The state was written
  by a newer bobi and may contain structures this version cannot
  safely read or write.
"""

from __future__ import annotations

from pathlib import Path

from bobi import paths

# Bump this when the on-disk state layout changes in a way that older
# code cannot safely ignore.
CURRENT_FORMAT_VERSION = 1


class StateVersionError(RuntimeError):
    """Raised when on-disk state format is newer than this code supports."""


def format_version_path(root: Path | None = None) -> Path:
    """Path to the format_version marker (no side effects)."""
    return paths.state_path(root) / "format_version"


def ensure_state_version(root: Path | None = None) -> None:
    """Write or validate the state format version marker.

    Called once during manager startup, after ``state_dir()`` has created
    the state directory.

    - Missing file → first run (or upgrade from pre-versioned state):
      write the current version.
    - On-disk == current → no-op.
    - On-disk < current → (future) run migrations, then stamp current
      version. Today there are no migrations, so just overwrite.
    - On-disk > current → raise ``StateVersionError``.
    """
    fv = format_version_path(root)

    if fv.exists():
        raw = fv.read_text().strip()
        try:
            on_disk = int(raw)
        except ValueError:
            raise StateVersionError(
                f"Corrupt state format version at {fv} — expected an "
                f"integer, got {raw!r}. Remove the file to reinitialize "
                f"(may lose state if the format actually changed)."
            )

        if on_disk > CURRENT_FORMAT_VERSION:
            raise StateVersionError(
                f"State directory was written by a newer bobi "
                f"(format version {on_disk}, this build supports "
                f"{CURRENT_FORMAT_VERSION}). Upgrade bobi before "
                f"starting, or the on-disk state may be corrupted."
            )

        if on_disk == CURRENT_FORMAT_VERSION:
            return  # nothing to do

        # on_disk < CURRENT_FORMAT_VERSION — future migration hook.
        # Today there is only version 1, so no migrations exist yet.

    # Write (or overwrite after migration).
    fv.parent.mkdir(parents=True, exist_ok=True)
    fv.write_text(f"{CURRENT_FORMAT_VERSION}\n")
