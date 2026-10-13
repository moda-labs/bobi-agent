"""Operator-authenticated client for the Worker Admin command route."""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from bobi import http

ALIASES = {
    "metrics_summary": "usage",
    "metrics_session": "usage_session",
    "metrics_turn": "usage_turn",
}

DRILLDOWN_ALIASES = frozenset(ALIASES) - {"metrics_summary"}
MIN_DRILLDOWN_SUPERVISOR = (0, 4, 0)
# Arguments only the fine-grained part of `usage` reads. A supervisor older than
# MIN_DRILLDOWN_SUPERVISOR ignores them and returns unfiltered legacy totals.
SUMMARY_FILTER_KEYS = frozenset({
    "session", "session_id", "session_name", "model", "run_key",
    "experiment_id", "variant_id", "routing", "group_by",
})


class AdminClientError(RuntimeError):
    pass


def _segment(value: str) -> str:
    return quote(value, safe="")


def _version_tuple(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple((list(map(int, parts)) + [0, 0, 0])[:3])


def _unsupported_supervisor_version(
    *, detail_url: str, headers: dict[str, str], timeout: float,
) -> str | None:
    """Return the heartbeat's supervisor version when it predates fine-grained metrics."""
    try:
        response = http.get(detail_url, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        raise AdminClientError(f"admin version preflight failed: {exc}") from None
    if response.status_code >= 400:
        raise AdminClientError(
            f"admin version preflight rejected ({response.status_code}): {response.text}"
        )
    try:
        detail = response.json()
    except ValueError as exc:
        raise AdminClientError("admin version preflight returned invalid JSON") from exc
    supervisor = detail.get("supervisor") if isinstance(detail, dict) else None
    version_value = supervisor.get("version") if isinstance(supervisor, dict) else None
    version = _version_tuple(version_value)
    if version is None or version < MIN_DRILLDOWN_SUPERVISOR:
        return version_value if isinstance(version_value, str) else "unknown"
    return None


def _mark_summary_unfiltered(view: dict) -> None:
    """Say on the response that the filters were not applied.

    The marker goes beside the legacy fold when the supervisor already answered,
    and on the envelope otherwise, so a pending view still carries it.
    """
    marker = {"code": "supervisor_too_old"}
    result = view.get("result")
    usage = result.get("usage") if isinstance(result, dict) else None
    if isinstance(usage, dict):
        usage["fine_grained_unavailable"] = marker
    else:
        view["fine_grained_unavailable"] = marker

def _remaining_timeout(deadline: float, stage: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AdminClientError(f"admin command timed out before {stage}")
    return remaining


def run_admin_command(
    *,
    base_url: str,
    token: str,
    fleet: str,
    instance: str,
    alias: str,
    args: dict,
    wait: bool = False,
    timeout: float = 10.0,
    poll_interval: float = 0.25,
) -> dict:
    command = ALIASES.get(alias)
    if command is None:
        raise AdminClientError(f"unknown metrics command {alias}")
    from bobi.events.server import _is_loopback_or_tls

    if not _is_loopback_or_tls(base_url):
        # Every request carries the operator token; never send it in cleartext off-host.
        raise AdminClientError("the admin URL must use https unless it is a loopback address")
    base = base_url.rstrip("/")
    auth = {"Authorization": f"Bearer {token}"}
    instance_path = f"/fleet/instances/{_segment(fleet)}/{_segment(instance)}"
    deadline = time.monotonic() + timeout
    filtered_summary = alias == "metrics_summary" and any(
        args.get(key) is not None for key in SUMMARY_FILTER_KEYS
    )
    summary_unfiltered = False
    if alias in DRILLDOWN_ALIASES or filtered_summary:
        old_version = _unsupported_supervisor_version(
            detail_url=base + instance_path,
            headers=auth,
            timeout=_remaining_timeout(deadline, "version preflight"),
        )
        if old_version is not None and alias in DRILLDOWN_ALIASES:
            raise AdminClientError(
                "fine-grained metrics are unavailable: supervisor version "
                f"{old_version}; version 0.4.0 or newer is required"
            )
        # The legacy totals are still valid, so a summary is sent anyway.
        summary_unfiltered = old_version is not None
    view = _submit_command(
        url=base + instance_path + "/commands",
        headers=auth,
        body={"command": command, "args": args},
        wait=wait,
        deadline=deadline,
        poll_interval=poll_interval,
    )
    if summary_unfiltered and view.get("status") != "error":
        _mark_summary_unfiltered(view)
    return view


def _submit_command(
    *,
    url: str,
    headers: dict[str, str],
    body: dict,
    wait: bool,
    deadline: float,
    poll_interval: float,
) -> dict:
    try:
        response = http.post(
            url,
            headers=headers,
            json=body,
            timeout=_remaining_timeout(deadline, "command submission"),
        )
    except httpx.RequestError as exc:
        raise AdminClientError(f"admin command failed: {exc}") from None
    if response.status_code >= 400:
        raise AdminClientError(f"admin command rejected ({response.status_code}): {response.text}")
    try:
        view = response.json()
    except ValueError as exc:
        raise AdminClientError("admin command returned invalid JSON") from exc
    if not isinstance(view, dict):
        raise AdminClientError("admin command returned a non-object response")
    if not wait:
        return view
    command_id = str(view.get("command_id") or "")
    if not command_id:
        raise AdminClientError("admin response did not include command_id")
    result_url = url + f"/{_segment(command_id)}"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return view
        try:
            polled = http.get(result_url, headers=headers, timeout=remaining)
        except httpx.RequestError as exc:
            raise AdminClientError(f"admin poll failed: {exc}") from None
        if polled.status_code == 200:
            try:
                view = polled.json()
            except ValueError as exc:
                raise AdminClientError("admin poll returned invalid JSON") from exc
            if not isinstance(view, dict):
                raise AdminClientError("admin poll returned a non-object response")
            if view.get("status") in {"done", "error"}:
                return view
        elif polled.status_code != 404 and polled.status_code < 500:
            raise AdminClientError(f"admin poll rejected ({polled.status_code}): {polled.text}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return view
        time.sleep(min(poll_interval, remaining))
