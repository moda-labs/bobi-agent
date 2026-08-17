"""Live Slack identity checks for the MOD-309 missing-scope path.

Set ``SLACK_NEGATIVE_BOT_TOKEN`` to a throwaway app installation that omits
``users:read``. The test verifies both Slack's real ``missing_scope`` response
and Bobi's fail-closed handling without starting Socket Mode.
"""

import os

import httpx
import pytest

from bobi.slack import SlackAppIdentityError, require_app_identity


pytestmark = [
    pytest.mark.live,
    pytest.mark.timeout(30),
    pytest.mark.skipif(
        not os.environ.get("SLACK_NEGATIVE_BOT_TOKEN"),
        reason="set SLACK_NEGATIVE_BOT_TOKEN for the no-users:read app",
    ),
]


def test_missing_users_read_fails_closed_against_real_slack():
    token = os.environ["SLACK_NEGATIVE_BOT_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}

    auth = httpx.post(
        "https://slack.com/api/auth.test", headers=headers, timeout=15,
    ).json()
    assert auth.get("ok"), (
        "SLACK_NEGATIVE_BOT_TOKEN rejected by auth.test: "
        f"{auth.get('error', 'unknown error')}"
    )

    bots = httpx.get(
        "https://slack.com/api/bots.info",
        params={"bot": auth["bot_id"]},
        headers=headers,
        timeout=15,
    ).json()
    assert bots.get("ok") is False
    assert bots.get("error") == "missing_scope"

    with pytest.raises(SlackAppIdentityError, match="users:read"):
        require_app_identity(token)
