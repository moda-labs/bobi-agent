"""Slack helpers for system-side notifications and setup.

Agent replies no longer go through here: since #190 Phase 2 they flow
through the event server's channel gateway (``bobi/events/gateway.py``),
which owns formatting and delivery. What remains is the direct-token
path used by system notifications (watchdog, monitors, workflow
orchestrator, auth bootstrap) and channel-reference resolution for setup.

All errors are raised unless documented otherwise — callers decide
how to handle failures.
"""

from __future__ import annotations

import logging
import re

from bobi import http as pooled

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

TRUNCATION_LIMIT = 3000
TRUNCATION_SUFFIX = "\n_(truncated)_"
BOLD_MARKER = "\x02"
STRIKE_MARKER = "\x03"


def _is_markdown_table_row(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and len(stripped.split("|")) >= 3


def _is_markdown_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    cells = [cell.strip() for cell in stripped.split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _wrap_markdown_tables(text: str) -> str:
    """Wrap markdown tables in code blocks so Slack does not garble them."""
    lines = text.splitlines()
    wrapped: list[str] = []
    i = 0
    in_code_block = False
    while i < len(lines):
        if lines[i].strip().startswith("```"):
            in_code_block = not in_code_block
            wrapped.append(lines[i])
            i += 1
            continue
        if (
            not in_code_block
            and i + 1 < len(lines)
            and _is_markdown_table_row(lines[i])
            and _is_markdown_table_separator(lines[i + 1])
        ):
            table = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and _is_markdown_table_row(lines[i]):
                table.append(lines[i])
                i += 1
            wrapped.extend(["```", *table, "```"])
            continue
        wrapped.append(lines[i])
        i += 1
    return "\n".join(wrapped)


def _convert_markdown_line(line: str) -> str:
    line = re.sub(r'^#{1,6}\s+(.+)$', rf'{BOLD_MARKER}\1{BOLD_MARKER}', line)
    line = re.sub(
        r'\*\*(.+?)\*\*',
        lambda match: f"{BOLD_MARKER}{match.group(1)}{BOLD_MARKER}",
        line,
    )
    line = re.sub(
        r'~~(.+?)~~',
        lambda match: f"{STRIKE_MARKER}{match.group(1)}{STRIKE_MARKER}",
        line,
    )
    line = re.sub(r'^( *)[-*] ', r'\1• ', line)
    return re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', line)


def _convert_markdown_outside_code_blocks(text: str) -> str:
    lines = text.split("\n")
    converted: list[str] = []
    in_code_block = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            converted.append(line)
        elif in_code_block:
            converted.append(line)
        else:
            converted.append(_convert_markdown_line(line))
    return "\n".join(converted)


def _has_open_code_fence(text: str) -> bool:
    in_code_block = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
    return in_code_block


def _outside_code_marker_locations(text: str, marker: str) -> list[tuple[int, int]]:
    locations: list[tuple[int, int]] = []
    in_code_block = False
    for line_index, line in enumerate(text.split("\n")):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        start = 0
        line_locations: list[int] = []
        while True:
            marker_index = line.find(marker, start)
            if marker_index < 0:
                break
            line_locations.append(marker_index)
            start = marker_index + len(marker)
        locations.extend((line_index, marker_index) for marker_index in line_locations)
    return locations


def _remove_outside_code_marker(
    text: str,
    marker: str,
    location: tuple[int, int],
) -> str:
    lines = text.split("\n")
    line_index, marker_index = location
    line = lines[line_index]
    lines[line_index] = line[:marker_index] + line[marker_index + len(marker):]
    return "\n".join(lines)


def _truncate_slack_message(text: str) -> str:
    """Truncate without cutting through words or leaving open mrkdwn markers."""
    if len(text) <= TRUNCATION_LIMIT:
        return text

    body = text[:TRUNCATION_LIMIT]
    boundary = max(body.rfind(" "), body.rfind("\n"), body.rfind("\t"))
    if boundary >= int(TRUNCATION_LIMIT * 0.8):
        body = body[:boundary]
    body = body.rstrip()

    if _has_open_code_fence(body):
        body += "\n```"
    for marker in ("`", BOLD_MARKER, STRIKE_MARKER):
        locations = _outside_code_marker_locations(body, marker)
        if len(locations) % 2:
            body = _remove_outside_code_marker(body, marker, locations[-1])

    return body + TRUNCATION_SUFFIX


def format_slack_message(text: str) -> str:
    """Convert markdown to Slack mrkdwn and truncate if needed."""
    # Escaped newlines from shell invocations
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    text = _wrap_markdown_tables(text)
    text = _convert_markdown_outside_code_blocks(text)
    text = _truncate_slack_message(text)
    return text.replace(BOLD_MARKER, "*").replace(STRIKE_MARKER, "~")


# ---------------------------------------------------------------------------
# Post / Update
# ---------------------------------------------------------------------------

def _slack_api(
    endpoint: str,
    token: str,
    payload: dict,
    *,
    timeout: float = 10,
) -> dict:
    """POST to a Slack Web API endpoint and return the parsed response.

    Raises ``RuntimeError`` for non-ok responses.
    """
    resp = pooled.post(
        f"https://slack.com/api/{endpoint}",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    result = resp.json()

    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error', 'unknown')}")

    return result


def _workspace_label(token: str, *, timeout: float = 10) -> str:
    """Return a human-readable Slack workspace label from auth.test."""
    result = _slack_api("auth.test", token, {}, timeout=timeout)
    team = result.get("team") or "unknown workspace"
    team_id = result.get("team_id") or "unknown team"
    return f"{team} ({team_id})"


def _user_matches(member: dict, handle: str) -> bool:
    """True when a Slack user record matches a human-entered @handle."""
    return (member.get("name") or "").strip().lstrip("@").lower() == handle


def _resolve_im_channel_id(
    token: str,
    ref: str,
    *,
    timeout: float = 10,
) -> str:
    """Resolve ``@handle`` to a bot DM channel ID in the token's workspace."""
    workspace = _workspace_label(token, timeout=timeout)
    raw_handle = ref.strip().lstrip("@")
    handle = raw_handle.lower()
    if not handle:
        raise RuntimeError(
            f"Slack user reference '{ref}' is empty in workspace {workspace}."
        )

    if re.fullmatch(r"[UW][A-Z0-9]{6,}", raw_handle):
        user_id = raw_handle
    else:
        matches: list[dict] = []
        deleted_matches: list[dict] = []
        cursor = ""
        while True:
            params: dict = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            resp = pooled.client().get(
                "https://slack.com/api/users.list",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
            result = resp.json()
            if not result.get("ok"):
                err = result.get("error", "unknown")
                raise RuntimeError(
                    f"Slack user '{ref}' could not be resolved in {workspace}: "
                    f"users.list failed with {err}. The bot needs users:read."
                )
            for member in result.get("members", []):
                if _user_matches(member, handle):
                    if member.get("deleted"):
                        deleted_matches.append(member)
                    else:
                        matches.append(member)
            cursor = (result.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break

        if len(matches) > 1:
            ids = ", ".join(m.get("id", "?") for m in matches)
            raise RuntimeError(
                f"Slack user '{ref}' is ambiguous in {workspace}; matched "
                f"multiple active users ({ids}). Use the Slack user ID instead."
            )
        if not matches and deleted_matches:
            ids = ", ".join(m.get("id", "?") for m in deleted_matches)
            raise RuntimeError(
                f"Slack user '{ref}' in {workspace} is deleted ({ids}); choose "
                "an active user."
            )
        if not matches:
            raise RuntimeError(
                f"Slack user '{ref}' was not found in {workspace}. Check the "
                "handle and make sure it belongs to the bot token's workspace."
            )
        user_id = matches[0]["id"]

    try:
        result = _slack_api(
            "conversations.open",
            token,
            {"users": user_id},
            timeout=timeout,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Slack user '{ref}' resolved in {workspace}, but the bot could not "
            f"open a DM: {exc}. The bot needs im:write."
        ) from exc
    channel_id = ((result.get("channel") or {}).get("id") or "").strip()
    if not channel_id:
        raise RuntimeError(
            f"Slack user '{ref}' resolved in {workspace}, but conversations.open "
            "did not return a DM channel ID."
        )
    log.info("Resolved %s in %s to %s.", ref, workspace, channel_id)
    return channel_id


def resolve_channel_id(token: str, channel: str, *, timeout: float = 10) -> str:
    """Resolve a Slack channel reference to its channel ID.

    Accepts either an ID (``C…``/``G…``/``D…``, returned unchanged), ``@handle``
    for the bot's DM with a Slack user, or a human channel name (with or without
    a leading ``#``), looked up via ``conversations.list``. Matches public and
    private channels, falling back to public-only if the token lacks
    ``groups:read``. Lets the config carry ``#codex-test`` or ``@zach`` instead
    of opaque Slack IDs. Raises ``RuntimeError`` if a reference can't be resolved.
    """
    ref = (channel or "").strip()
    if not ref:
        return ref
    # Already an ID? IDs start with C/G/D and are uppercase alphanumerics; a
    # '#'-prefixed value is always a name.
    if not ref.startswith("#") and re.fullmatch(r"[CGD][A-Z0-9]{6,}", ref):
        return ref
    if ref.startswith("@"):
        return _resolve_im_channel_id(token, ref, timeout=timeout)
    want = ref.lstrip("#").lower()

    types = "public_channel,private_channel"
    cursor = ""
    while True:
        params: dict = {"types": types, "limit": 1000, "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        resp = pooled.client().get(
            "https://slack.com/api/conversations.list",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        result = resp.json()
        if not result.get("ok"):
            err = result.get("error", "unknown")
            # Private channels need groups:read; degrade to public-only rather
            # than fail when the app wasn't granted it.
            if err == "missing_scope" and types != "public_channel":
                types, cursor = "public_channel", ""
                continue
            raise RuntimeError(f"Slack API error: {err}")
        for ch in result.get("channels", []):
            if (ch.get("name") or "").lower() == want:
                return ch["id"]
        cursor = (result.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    raise RuntimeError(
        f"Slack channel '#{want}' not found — is the bot a member? "
        "(a private channel also needs the groups:read scope)."
    )


def post_slack_message(
    token: str,
    channel: str,
    text: str,
    thread_ts: str = "",
    *,
    timeout: float = 10,
) -> dict:
    """Post a message to Slack and return the API response dict.

    Raises on network errors or non-ok Slack responses.
    """
    text = format_slack_message(text)

    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    return _slack_api("chat.postMessage", token, payload, timeout=timeout)
