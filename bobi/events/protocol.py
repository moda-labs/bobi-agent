"""Compatibility contract for the Bobi client/event-server handshake."""

from __future__ import annotations

from collections.abc import Mapping


EVENT_PROTOCOL = {"current": 1, "minimum": 1}
_LEGACY_PROTOCOL = {"current": 1, "minimum": 1}
_MAX_SAFE_INTEGER = (1 << 53) - 1


class EventProtocolError(RuntimeError):
    """Base class for event protocol negotiation failures."""


class InvalidEventProtocol(EventProtocolError):
    """A peer advertised malformed event protocol metadata."""


class IncompatibleEventProtocol(EventProtocolError):
    """The Bobi client and event server support disjoint protocol ranges."""


def protocol_payload() -> dict[str, int]:
    """Return a fresh wire representation of this client's supported range."""
    return dict(EVENT_PROTOCOL)


def _parse_protocol(value: object, *, peer: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise InvalidEventProtocol(
            f"{peer} returned invalid event protocol metadata; expected "
            "positive integer minimum/current fields"
        )

    minimum = value.get("minimum")
    current = value.get("current")
    if (
        type(minimum) is not int
        or type(current) is not int
        or minimum < 1
        or current < 1
        or minimum > _MAX_SAFE_INTEGER
        or current > _MAX_SAFE_INTEGER
        or minimum > current
    ):
        raise InvalidEventProtocol(
            f"{peer} returned invalid event protocol metadata; expected "
            "positive integer minimum/current fields with minimum <= current"
        )
    return {"minimum": minimum, "current": current}


def validate_server_response(payload: object) -> dict[str, int]:
    """Validate a successful server response and return its supported range.

    A missing ``protocol`` member is the legacy v1 response shape. Unknown
    fields are ignored so additive changes remain compatible.
    """
    if not isinstance(payload, Mapping):
        raise InvalidEventProtocol(
            "event server returned invalid event protocol response; expected a JSON object"
        )

    remote = (
        dict(_LEGACY_PROTOCOL)
        if "protocol" not in payload
        else _parse_protocol(payload["protocol"], peer="event server")
    )
    if (
        EVENT_PROTOCOL["current"] < remote["minimum"]
        or remote["current"] < EVENT_PROTOCOL["minimum"]
    ):
        raise IncompatibleEventProtocol(
            "event protocol ranges do not overlap "
            f"(client {EVENT_PROTOCOL['minimum']}..{EVENT_PROTOCOL['current']}; "
            f"server {remote['minimum']}..{remote['current']}); upgrade or "
            "downgrade Bobi or the event server"
        )
    return remote


def raise_for_protocol_error(status_code: int, payload: object) -> None:
    """Raise the typed client error represented by a server rejection."""
    error = payload.get("error") if isinstance(payload, Mapping) else None
    detail = payload.get("detail") if isinstance(payload, Mapping) else None
    if error == "invalid_protocol":
        message = str(detail) if detail else "invalid event protocol metadata"
        raise InvalidEventProtocol(message)
    if error == "incompatible_protocol" or status_code == 426:
        if isinstance(payload, Mapping) and "protocol" in payload:
            _parse_protocol(payload["protocol"], peer="event server")
        message = (
            str(detail)
            if detail
            else "event protocol ranges do not overlap; upgrade or downgrade "
            "Bobi or the event server"
        )
        raise IncompatibleEventProtocol(message)
