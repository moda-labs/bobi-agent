"""OTLP endpoint, header, protocol, and timeout resolution.

Spec-standard ``OTEL_*`` environment variables only - no ``Config`` field and
no ``agent.yaml`` schema change. Values are read through
``config.project_env()``'s process-env-over-``run/.env`` precedence, the same
precedence every other runtime credential uses.

Three outcomes, deliberately distinct so a box without observability does not
look broken:

* **unconfigured** - no endpoint at all (:class:`OtelUnconfigured`)
* **misconfigured** - an endpoint that cannot work (:class:`OtelMisconfigured`)
* **configured** - a :class:`SignalConfig`

Nothing here imports ``opentelemetry`` or ``httpx``, so ``otel check`` can
diagnose a box where the wire dependency is missing.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# Milliseconds. Matches bobi/http.py's _TIMEOUT so the exporter does not hang
# longer than every other outbound call in the framework.
DEFAULT_TIMEOUT_MS = 10000

BASE_ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
PROTOCOL_VAR = "OTEL_EXPORTER_OTLP_PROTOCOL"
TIMEOUT_VAR = "OTEL_EXPORTER_OTLP_TIMEOUT"
BASE_HEADERS_VAR = "OTEL_EXPORTER_OTLP_HEADERS"
RESOURCE_ATTRIBUTES_VAR = "OTEL_RESOURCE_ATTRIBUTES"
SERVICE_NAME_VAR = "OTEL_SERVICE_NAME"

# The one protocol this exporter speaks. OTLP makes binary protobuf a MUST for
# OTLP/HTTP and JSON only a MAY, and `grpc` needs a different transport
# entirely - so an explicit `grpc` is an error naming the var, never a silent
# HTTP attempt against a gRPC port.
SUPPORTED_PROTOCOL = "http/protobuf"

_SIGNAL_PATHS = {"metrics": "/v1/metrics", "logs": "/v1/logs"}


class OtelUnconfigured(Exception):
    """No OTLP endpoint is configured on this box."""


class OtelMisconfigured(Exception):
    """An OTLP setting is present but cannot produce a working export."""


@dataclass(frozen=True)
class SignalConfig:
    """A resolved, usable configuration for one OTLP signal."""

    signal: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_MS / 1000
    # True when origin-pinning suppressed configured headers because the
    # endpoint in use did not come from the same origin as the credential.
    credential_withheld: bool = False
    # The var the headers came from, for `otel check`'s name-only rendering.
    headers_var: str = ""


def signal_endpoint_var(signal: str) -> str:
    return f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT"


def signal_headers_var(signal: str) -> str:
    return f"OTEL_EXPORTER_OTLP_{signal.upper()}_HEADERS"


def parse_headers(raw: str) -> dict[str, str]:
    """Parse ``OTEL_EXPORTER_OTLP_HEADERS`` per the W3C Baggage rules.

    Split on ``,``, then on the **first** ``=`` only, then percent-decode and
    strip surrounding whitespace. Grafana Cloud's canonical value is
    ``Authorization=Basic <base64>``: the base64 carries ``=`` padding and the
    decoded value contains a space, so splitting on every ``=`` or skipping
    ``unquote`` both corrupt a real credential.
    """
    headers: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep:
            continue
        key = urllib.parse.unquote(key).strip()
        if not key:
            continue
        headers[key] = urllib.parse.unquote(value).strip()
    return headers


def _endpoint_from(env: dict[str, str], signal: str) -> str | None:
    """Resolve one signal's full URL from an env mapping, or None."""
    per_signal = (env.get(signal_endpoint_var(signal)) or "").strip()
    if per_signal:
        # Per-signal endpoints are used VERBATIM - the spec says the signal
        # path is already part of the value, and appending one again yields
        # `/v1/metrics/v1/metrics`.
        return per_signal
    base = (env.get(BASE_ENDPOINT_VAR) or "").strip()
    if not base:
        return None
    # Operators paste Grafana Cloud's documented `https://.../otlp/` with the
    # trailing slash; naive concatenation gives `//v1/metrics` -> 404.
    return base.rstrip("/") + _SIGNAL_PATHS[signal]


def _origin(url: str | None) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) for origin comparison, or None when unparseable."""
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return (parts.scheme.lower(), parts.hostname.lower(), port)


def _resolve_timeout(env: dict[str, str]) -> float:
    raw = (env.get(TIMEOUT_VAR) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_MS / 1000
    try:
        ms = int(raw)
    except ValueError:
        raise OtelMisconfigured(
            f"{TIMEOUT_VAR} must be a whole number of milliseconds, got {raw!r}."
        ) from None
    if ms <= 0:
        raise OtelMisconfigured(
            f"{TIMEOUT_VAR} must be a positive number of milliseconds, got {raw!r}."
        )
    return ms / 1000


def _check_protocol(env: dict[str, str]) -> None:
    protocol = (env.get(PROTOCOL_VAR) or "").strip()
    if protocol and protocol != SUPPORTED_PROTOCOL:
        raise OtelMisconfigured(
            f"{PROTOCOL_VAR}={protocol!r} is not supported; this exporter "
            f"speaks only {SUPPORTED_PROTOCOL!r}."
        )


def _dotenv_values(root: Path) -> dict[str, str]:
    from bobi import config as _config
    from bobi import paths

    return _config.parse_env_file(paths.env_path(root))


def _merged_env(root: Path) -> dict[str, str]:
    from bobi import config as _config

    return _config.project_env(root)


def resolve_config(root: Path, signal: str) -> SignalConfig:
    """Resolve one signal's endpoint, headers, and timeout.

    Raises :class:`OtelUnconfigured` when no endpoint is set anywhere, and
    :class:`OtelMisconfigured` when a present setting cannot work.

    **Credential origin-pinning.** ``project_env()`` makes process env beat
    ``run/.env``, so an attacker-controlled ``OTEL_EXPORTER_OTLP_ENDPOINT`` in
    the process environment would otherwise make bobi courier ``run/.env``'s
    write token to the attacker - a secret the agent never had to read. When
    the credential comes from ``run/.env`` but the endpoint in use does not
    share ``run/.env``'s origin, no configured headers are sent and the caller
    prints a withheld-credential notice.
    """
    if signal not in _SIGNAL_PATHS:
        raise ValueError(f"unknown OTLP signal: {signal!r}")

    dotenv = _dotenv_values(root)
    env = _merged_env(root)

    url = _endpoint_from(env, signal)
    if not url:
        raise OtelUnconfigured(
            f"No OTLP endpoint configured. Set {BASE_ENDPOINT_VAR} (or "
            f"{signal_endpoint_var(signal)}) in run/.env or the environment."
        )

    _check_protocol(env)
    timeout_s = _resolve_timeout(env)

    if _origin(url) is None:
        raise OtelMisconfigured(
            f"Resolved OTLP endpoint {url!r} is not an absolute http(s) URL. "
            f"Set {BASE_ENDPOINT_VAR} to a full URL including the scheme."
        )

    headers_var = signal_headers_var(signal)
    if not (env.get(headers_var) or "").strip():
        headers_var = BASE_HEADERS_VAR
    raw_headers = (env.get(headers_var) or "").strip()
    headers = parse_headers(raw_headers) if raw_headers else {}

    # The credential is bobi's to protect only when it came from run/.env; a
    # caller supplying its own token to its own endpoint leaks nothing.
    from_dotenv = bool(raw_headers) and env.get(headers_var) == dotenv.get(headers_var)
    withheld = from_dotenv and _origin(url) != _origin(_endpoint_from(dotenv, signal))
    if withheld:
        headers = {}

    return SignalConfig(
        signal=signal,
        url=url,
        headers=headers,
        timeout_s=timeout_s,
        credential_withheld=withheld,
        headers_var=headers_var if raw_headers else "",
    )
