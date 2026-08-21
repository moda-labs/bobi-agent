"""Agent-authored OTLP telemetry (``bobi agent <name> otel``).

``resolve_config`` is imported eagerly - it is pure standard library, and
``otel check`` must be able to diagnose a box where the wire dependency is
missing. The exporters are resolved lazily through ``__getattr__`` so
``import bobi.otel`` never pulls in ``opentelemetry.proto`` (and through it the
``google.protobuf`` runtime, the dominant cold-start term) on a command that
only reads configuration.
"""

from __future__ import annotations

from bobi.otel.config import (  # noqa: F401
    OtelMisconfigured,
    OtelUnconfigured,
    SignalConfig,
    resolve_config,
)

__all__ = [
    "OtelMisconfigured",
    "OtelUnconfigured",
    "SignalConfig",
    "resolve_config",
    "export_log",
    "export_metric",
]

_LAZY = {"export_metric", "export_log"}


def __getattr__(name: str):
    if name in _LAZY:
        from bobi.otel import export

        return getattr(export, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
