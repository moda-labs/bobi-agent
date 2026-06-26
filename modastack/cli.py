"""Compatibility shim for stale ``modastack`` console-script wrappers."""

from bobi.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
