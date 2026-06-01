"""Slack responder — delivers manager responses to Slack.

Receives Slack events and the manager's response text, posts the
response back to the originating channel/thread. Pure Python, no LLM.

Later, this could be replaced with an agent that reasons about
context before replying.
"""

import json
import logging
import re
import urllib.error
import urllib.request

from modastack.config import GlobalConfig

log = logging.getLogger(__name__)


def _markdown_to_slack(text: str) -> str:
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    if len(text) > 3000:
        text = text[:3000] + '\n_(truncated)_'
    return text


def _post_to_slack(token: str, channel: str, text: str, thread_ts: str = "") -> bool:
    text = _markdown_to_slack(text)
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            log.warning(f"Slack reply error: {result.get('error', 'unknown')}")
            return False
        return True
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.warning(f"Slack reply failed: {e}")
        return False


class SlackResponder:
    """Routes manager responses back to Slack channels.

    Call `handle()` after the manager finishes a turn that included
    Slack events. The responder posts the response to each originating
    channel/thread.
    """

    def __init__(self):
        self._config = GlobalConfig.load()

    def handle(self, events: list[dict], response: str) -> None:
        if not response:
            return

        for event in events:
            if event.get("source") != "slack":
                continue

            data = event["data"]
            workspace = data.get("workspace", "")
            channel = data.get("channel", "")
            if not channel:
                continue

            token = self._config.slack_token_for(workspace)
            if not token:
                log.warning(f"No bot token for workspace {workspace}")
                continue

            thread_ts = data.get("thread_ts", "")
            if not thread_ts and event["type"] == "slack.mention":
                thread_ts = data.get("ts", "")

            ok = _post_to_slack(token, channel, response, thread_ts)
            if ok:
                user_id = data.get("user_id", "")
                log.info(f"Slack reply sent to {channel}" +
                         (f" thread {thread_ts}" if thread_ts else "") +
                         (f" (user {user_id})" if user_id else ""))
