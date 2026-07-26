"""Tests for slack_manifest — manifest rendering, JSON conversion, deep link.

The manifest is the one source of truth for the scopes + events the bobi
Slack adapter (event-server/core/src/adapters/chat-sdk-slack.ts) consumes. These tests pin
that the rendered manifest stays wired to the event server's /webhooks/slack
path and carries every event the adapter normalizes.
"""

import json
import string
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from click.testing import CliRunner

from bobi import slack_manifest
from bobi.cli import main
from bobi.config import DEFAULT_EVENT_SERVER
from bobi.slack_manifest import (
    TEMPLATE_PATH,
    WEBHOOK_PATH,
    create_app_url,
    event_server_error,
    manifest_to_dict,
    manifest_to_json,
    render_manifest,
    webhook_url,
)

EVENT_SERVER = "https://my-worker.workers.dev"

# Every event the Slack adapter turns into a bobi event must be subscribed.
EXPECTED_BOT_EVENTS = {
    "app_mention",       # -> slack.mention
    "message.im",        # -> slack.dm
    "message.mpim",      # -> slack.dm
    "message.channels",  # -> slack.thread_reply (public)
    "message.groups",    # -> slack.thread_reply (private)
}

# Scopes those events require, plus the scopes the outbound CLI tools need.
EXPECTED_BOT_SCOPES = {
    "app_mentions:read", "channels:history", "channels:read", "chat:write",
    "files:read", "files:write", "groups:history", "groups:read", "im:history",
    "im:read", "im:write", "mpim:history", "users:read",
}


def test_render_substitutes_name_and_request_url():
    out = render_manifest("Eng Bot", EVENT_SERVER)
    data = yaml.safe_load(out)
    assert data["display_information"]["name"] == "Eng Bot"
    assert data["features"]["bot_user"]["display_name"] == "Eng Bot"
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_default_render_is_byte_identical_to_raw_template_substitution():
    """An ordinary name and URL are substituted verbatim - the escaping adds no
    quoting noise to the manifest a user reads."""
    expected = string.Template(TEMPLATE_PATH.read_text()).safe_substitute(
        APP_NAME="Eng Bot",
        REQUEST_URL=f"{EVENT_SERVER}{WEBHOOK_PATH}",
    )

    assert render_manifest("Eng Bot", EVENT_SERVER) == expected
    assert render_manifest(
        "Eng Bot", EVENT_SERVER, socket_mode=False,
    ) == expected


@pytest.mark.parametrize("app_name", [
    pytest.param("Bobi: Staging", id="colon-breaks-the-mapping"),
    pytest.param("Bobi #1", id="hash-starts-a-comment"),
    pytest.param("yes", id="parses-as-a-boolean"),
    pytest.param("null", id="parses-as-none"),
    pytest.param("- Bobi", id="parses-as-a-sequence"),
    pytest.param("{Bobi}", id="parses-as-a-flow-mapping"),
    pytest.param("*Bobi", id="parses-as-an-alias"),
    pytest.param("'Bobi'", id="already-quoted"),
    pytest.param('"Bobi"', id="already-double-quoted"),
    pytest.param("2026-07-25", id="parses-as-a-date"),
])
def test_app_name_with_yaml_special_characters_round_trips(app_name):
    """A user-typed display name is data, not YAML: it must survive rendering
    verbatim instead of breaking the parse or silently changing the app name."""
    data = manifest_to_dict(render_manifest(app_name, EVENT_SERVER))
    assert data["display_information"]["name"] == app_name
    assert data["features"]["bot_user"]["display_name"] == app_name
    # The rest of the manifest is unharmed by the substitution.
    assert set(data["oauth_config"]["scopes"]["bot"]) == EXPECTED_BOT_SCOPES
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_app_name_cannot_inject_manifest_keys():
    """A name carrying YAML structure renders as data, never as new keys."""
    app_name = "Bobi\n  is_admin: true"
    data = manifest_to_dict(render_manifest(app_name, EVENT_SERVER))
    assert data["display_information"]["name"] == app_name
    assert "is_admin" not in data["display_information"]
    assert "is_admin" not in data["features"]["bot_user"]


def test_multiline_app_name_stays_one_scalar():
    """A name PyYAML would otherwise spread over several lines is escaped
    inline, so the surrounding manifest keys keep their meaning."""
    app_name = "Bobi\nStaging"
    rendered = render_manifest(app_name, EVENT_SERVER)
    data = manifest_to_dict(rendered)
    assert data["display_information"]["name"] == app_name
    assert data["display_information"]["description"].startswith("bobi agent")


# The two proven ways a hostile event server URL rewrote the manifest: the
# template puts it in a bare scalar position, so YAML read it as syntax.
HOSTILE_EVENT_SERVERS = [
    pytest.param(
        "https://x #staging",
        id="comment-swallows-the-webhook-path",
    ),
    pytest.param(
        "https://ok.example.com\n"
        "features:\n"
        "  bot_user:\n"
        "    display_name: EVIL\n"
        "    always_online: false\n"
        "settings:\n"
        "  event_subscriptions:\n"
        "    request_url: https://attacker.example.com",
        id="newline-injects-a-second-features-key",
    ),
]


@pytest.mark.parametrize("event_server", HOSTILE_EVENT_SERVERS)
def test_hostile_event_server_never_reaches_the_manifest(event_server):
    """An event server URL is data, not YAML.

    ``https://x #staging`` rendered ``request_url: https://x`` - YAML ate
    ``/webhooks/slack`` as a comment, so the Slack app pointed somewhere else
    and the bot silently received nothing, with no error anywhere. An embedded
    newline spliced in a second top-level ``features:`` key, which PyYAML
    resolves last-wins, replacing the real bot-user config. Neither is a URL
    Slack could reach, so both are refused where the request URL is built.

    Socket mode strips the request URL out, so it does not validate the value -
    what matters there is the weaker but sufficient property that the hostile
    string cannot reach the output at all.
    """
    with pytest.raises(ValueError):
        render_manifest("Bot", event_server)

    rendered = render_manifest("Bot", event_server, socket_mode=True)
    assert event_server not in rendered
    assert "request_url" not in rendered
    data = manifest_to_dict(rendered)
    assert data["features"]["bot_user"]["display_name"] == "Bot"
    assert data["settings"]["socket_mode_enabled"] is True


@pytest.mark.parametrize("event_server", HOSTILE_EVENT_SERVERS)
def test_request_url_substitution_site_escapes_the_whole_url(event_server):
    """Defence in depth, and the reason the template stopped concatenating.

    The URL check refuses both of these outright, but the substitution site has
    to be safe on its own: the template takes the WHOLE request URL as one
    escaped scalar, so even a value that got past the check renders as data.
    Quoting only the event-server half could never do this - the ``request_url``
    scalar continued past the closing quote into ``/webhooks/slack``.
    """
    hostile = f"{event_server}{WEBHOOK_PATH}"
    rendered = string.Template(TEMPLATE_PATH.read_text()).safe_substitute(
        APP_NAME="Bot",
        REQUEST_URL=slack_manifest._single_line_scalar(hostile, "request URL"),
    )
    data = manifest_to_dict(rendered)
    assert data["settings"]["event_subscriptions"]["request_url"] == hostile
    assert data["features"]["bot_user"]["display_name"] == "Bot"
    assert data["features"]["app_home"]["messages_tab_enabled"] is True
    assert set(data["settings"]["event_subscriptions"]["bot_events"]) == (
        EXPECTED_BOT_EVENTS)


@pytest.mark.parametrize("event_server", [
    pytest.param("my-worker.workers.dev", id="no-scheme"),
    pytest.param("ftp://my-worker.workers.dev", id="not-http"),
    pytest.param("https://", id="no-host"),
    pytest.param("", id="empty"),
    pytest.param("https://ok.example.com\tstaging", id="control-character"),
])
def test_invalid_event_server_url_is_rejected(event_server):
    """Slack has to reach this host from the internet. A value that isn't an
    absolute http(s) URL can only produce a request URL that quietly doesn't
    work, so it's refused where it enters."""
    with pytest.raises(ValueError):
        render_manifest("Bot", event_server)


SCHEME_LESS = [
    pytest.param("my-worker.workers.dev", id="bare-host"),
    # urlsplit reads `localhost` as the SCHEME here, so the value has neither a
    # usable scheme nor a netloc.
    pytest.param("localhost:8080", id="host-and-port"),
    pytest.param("//my-worker.workers.dev", id="protocol-relative"),
]


@pytest.mark.parametrize("event_server", SCHEME_LESS)
def test_scheme_less_host_is_told_to_add_the_scheme(event_server):
    """The natural typo - a host with no scheme - has to name its own fix.

    urlsplit puts a scheme-less authority in `path`, never in `netloc`, so a
    netloc-first check answers "needs a host" for a value that is nothing BUT a
    host: `bobi create-slack-bot --event-server my-worker.workers.dev` would
    exit telling the user they omitted the thing they just supplied, and the
    setup UI renders the same string verbatim as its 400 error.
    """
    problem = event_server_error(event_server)
    assert problem, "a scheme-less host is not a usable event server"
    assert "needs a host" not in problem
    assert "absolute http(s) URL" in problem
    assert event_server in problem


@pytest.mark.parametrize("event_server", SCHEME_LESS)
def test_scheme_less_host_is_told_to_use_https_when_public(event_server):
    """The setup UI's field is specifically a PUBLIC server, so its answer to
    the same typo is the https:// guidance, not a missing-host report."""
    assert (event_server_error(event_server, require_https=True)
            == "use a public https:// event server URL")


@pytest.mark.parametrize("event_server", [
    pytest.param("https://", id="scheme-only"),
    pytest.param("https:///webhooks", id="scheme-and-path"),
])
def test_genuinely_host_less_url_still_reports_the_missing_host(event_server):
    """The host check keeps its own case: a value that names a scheme and no
    authority really is missing a host."""
    assert "needs a host" in event_server_error(event_server)
    assert "needs a host" in event_server_error(event_server,
                                                require_https=True)


@pytest.mark.parametrize("event_server", [
    pytest.param("my-worker.workers.dev", id="no-scheme"),
    pytest.param("", id="empty"),
    pytest.param("https://ok.example.com\tstaging", id="control-character"),
])
def test_socket_mode_ignores_an_unusable_event_server(event_server):
    """Socket mode emits no request URL, so the event server is not its input.

    It comes from the installed team config rather than the user, so refusing
    it here would block a manifest that never contains it - on a value the
    operator did not type and cannot see is being used.
    """
    rendered = render_manifest("Bot", event_server, socket_mode=True)
    assert "request_url" not in rendered
    assert manifest_to_dict(rendered)["settings"]["socket_mode_enabled"] is True


@pytest.mark.parametrize("event_server", [
    pytest.param("https://user:pw@x.example.com", id="colon-in-authority"),
    pytest.param("https://x.example.com/a,b{c}", id="flow-indicators"),
    pytest.param("https://x.example.com/'quoted'", id="quotes"),
])
def test_url_shaped_event_server_round_trips_verbatim(event_server):
    """Independently of the URL check: whatever reaches the substitution site
    is escaped, so a valid URL carrying YAML-significant characters lands in
    the manifest byte-for-byte."""
    data = manifest_to_dict(render_manifest("Bot", event_server))
    assert (data["settings"]["event_subscriptions"]["request_url"]
            == f"{event_server}{WEBHOOK_PATH}")
    assert data["features"]["bot_user"]["display_name"] == "Bot"


@pytest.mark.parametrize("event_server", [
    pytest.param("https://x.example.com#staging", id="fragment"),
    pytest.param("https://x.example.com?env=prod", id="query"),
])
def test_event_server_carrying_a_query_or_fragment_is_refused(event_server):
    """The request URL is `event_server` + /webhooks/slack, and appending a
    path to a URL that already has a query or fragment buries the path inside
    it - a client asks for the HOST ROOT:

        https://x.example.com#staging + /webhooks/slack
            -> urlsplit(...).path == ''

    So Slack posts somewhere the adapter never sees and nothing errors, which
    is the silent failure this whole check exists to prevent. A previous
    version of this test asserted the broken concatenation was correct.
    """
    # Why it has to be refused rather than escaped: the naive concatenation
    # produces a URL whose path is empty.
    assert urlparse(f"{event_server}{WEBHOOK_PATH}").path == ""
    with pytest.raises(ValueError, match="query or fragment"):
        render_manifest("Bot", event_server)


def test_render_strips_trailing_slash_on_event_server():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER + "/"))
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_request_url_uses_webhook_path_constant():
    assert webhook_url(EVENT_SERVER) == EVENT_SERVER + WEBHOOK_PATH
    assert webhook_url(EVENT_SERVER + "/") == EVENT_SERVER + WEBHOOK_PATH


def test_manifest_carries_all_adapter_events():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER))
    events = set(data["settings"]["event_subscriptions"]["bot_events"])
    assert events == EXPECTED_BOT_EVENTS


def test_manifest_carries_required_scopes():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER))
    scopes = set(data["oauth_config"]["scopes"]["bot"])
    assert scopes == EXPECTED_BOT_SCOPES


def test_manifest_uses_http_events_not_socket_mode():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER))
    assert data["settings"]["socket_mode_enabled"] is False


def test_socket_mode_manifest_removes_webhook_without_changing_contract():
    rendered = render_manifest("Bot", EVENT_SERVER, socket_mode=True)
    data = manifest_to_dict(rendered)

    assert data["settings"]["socket_mode_enabled"] is True
    assert "request_url" not in data["settings"]["event_subscriptions"]
    assert set(
        data["settings"]["event_subscriptions"]["bot_events"]
    ) == EXPECTED_BOT_EVENTS
    assert set(data["oauth_config"]["scopes"]["bot"]) == EXPECTED_BOT_SCOPES
    assert "Socket Mode" in rendered
    assert "HTTP Events API, no Socket Mode" not in rendered


@pytest.mark.parametrize("needle,replacement", [
    pytest.param(
        "# message with thread_ts -> slack.thread_reply. HTTP Events API, no Socket Mode.",
        "# message with thread_ts -> slack.thread_reply.",
        id="missing-header-anchor",
    ),
    pytest.param(
        "    request_url: ${REQUEST_URL}",
        "    request_url: ${REQUEST_URL}\n"
        "    request_url: ${REQUEST_URL}",
        id="duplicate-request-url-anchor",
    ),
    pytest.param(
        "  socket_mode_enabled: false",
        "  socket_mode_enabled: inherited",
        id="missing-socket-flag-anchor",
    ),
])
def test_socket_mode_render_fails_loudly_on_template_anchor_drift(
    tmp_path, monkeypatch, needle, replacement,
):
    template = TEMPLATE_PATH.read_text()
    assert template.count(needle) == 1
    drifted = tmp_path / "slack-app.manifest.yaml"
    drifted.write_text(template.replace(needle, replacement))
    monkeypatch.setattr(slack_manifest, "TEMPLATE_PATH", drifted)

    with pytest.raises(ValueError):
        slack_manifest.render_manifest("Bot", EVENT_SERVER, socket_mode=True)


def test_manifest_to_json_roundtrips_to_same_dict():
    rendered = render_manifest("Bot", EVENT_SERVER)
    assert json.loads(manifest_to_json(rendered)) == manifest_to_dict(rendered)


def test_create_app_url_embeds_valid_manifest_json():
    rendered = render_manifest("Eng Bot", EVENT_SERVER)
    url = create_app_url(rendered)
    parsed = urlparse(url)
    assert parsed.netloc == "api.slack.com"
    qs = parse_qs(parsed.query)
    assert qs["new_app"] == ["1"]
    manifest = json.loads(qs["manifest_json"][0])
    assert manifest["display_information"]["name"] == "Eng Bot"
    assert (
        manifest["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_cli_works_without_an_install_and_falls_back_to_cloud(tmp_path):
    """The command must run before any install exists — no root, no
    error; it falls back to the bobi cloud event server."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--app-name", "Bot"])
    assert result.exit_code == 0, result.output
    assert f"{DEFAULT_EVENT_SERVER}/webhooks/slack" in result.output
    assert "api.slack.com/apps?new_app=1" in result.output


@pytest.mark.parametrize("event_server", [
    pytest.param("my-worker.workers.dev", id="no-scheme"),
    pytest.param("https://x #staging", id="space-in-host"),
])
def test_cli_reports_an_unusable_event_server_as_a_usage_error(
        tmp_path, event_server):
    """The renderer's ValueError has exactly one consumer - this command.

    A bare host is the natural typo on every path that supplies this value
    (--event-server, the interactive prompt, a project `event_server:`), so it
    has to read like every other bad input to the command rather than a Python
    traceback out of the CLI.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--app-name", "Bot",
            "--event-server", event_server, "--no-open", "--no-url",
        ])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "event server URL" in result.output


def test_cli_writes_json_file(tmp_path):
    runner = CliRunner()
    out = tmp_path / "manifest.json"
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--format", "json",
            "--event-server", EVENT_SERVER, "-o", str(out), "--no-url",
        ])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_cli_socket_mode_prints_manifest_and_app_token_next_steps(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--socket-mode", "--app-name", "Bot",
            "--event-server", EVENT_SERVER, "--no-open",
        ])

    assert result.exit_code == 0, result.output
    assert "socket_mode_enabled: true" in result.output
    assert "request_url" not in result.output
    assert "Request URL:" not in result.output
    assert EVENT_SERVER not in result.output
    assert "xapp-" in result.output
    assert "connections:write" in result.output
    assert "SLACK_APP_TOKEN" in result.output
    assert "credentials.app_token" in result.output
    assert "${SLACK_APP_TOKEN:-}" in result.output
    assert "doctor" in result.output


def test_cli_socket_mode_keeps_next_steps_when_create_link_is_hidden(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--socket-mode", "--app-name", "Bot",
            "--no-open", "--no-url",
        ])

    assert result.exit_code == 0, result.output
    assert "xapp-" in result.output
    assert "connections:write" in result.output
    assert "SLACK_APP_TOKEN" in result.output
    assert "doctor" in result.output


def test_cli_socket_mode_json_output_has_no_webhook(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--socket-mode", "--app-name", "Bot",
            "--format", "json", "--no-open", "--no-url",
        ])

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "request_url" not in manifest["settings"]["event_subscriptions"]
    assert "Next steps" not in result.stdout
    assert "SLACK_APP_TOKEN" in result.stderr


def test_cli_socket_mode_writes_transformed_output_file(tmp_path):
    runner = CliRunner()
    out = tmp_path / "socket.yaml"
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--socket-mode", "--app-name", "Bot",
            "--output", str(out), "--no-open", "--no-url",
        ])
        manifest = yaml.safe_load(out.read_text())

    assert result.exit_code == 0, result.output
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "request_url" not in manifest["settings"]["event_subscriptions"]


def test_cli_socket_mode_skips_interactive_event_server_prompt(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main,
            ["create-slack-bot", "--socket-mode", "--no-open", "--no-url"],
            input="My Bot\n",
        )

    assert result.exit_code == 0, result.output
    assert "Slack app display name" in result.output
    assert "Event server URL" not in result.output
    assert "public tunnel" not in result.output
    assert "name: My Bot" in result.output


def test_cli_socket_mode_create_link_embeds_socket_manifest(
    monkeypatch, tmp_path,
):
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--socket-mode", "--app-name", "Bot",
            "--event-server", EVENT_SERVER, "--open",
        ])

    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    manifest = json.loads(parse_qs(urlparse(launched[0]).query)["manifest_json"][0])
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "request_url" not in manifest["settings"]["event_subscriptions"]


def test_cli_open_launches_browser(monkeypatch, tmp_path):
    """--open opens the one-click create link in the browser."""
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--open"])
    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    assert "api.slack.com/apps?new_app=1" in launched[0]
    assert "Opened the create page in your browser." in result.output


def test_cli_no_open_does_not_launch(monkeypatch, tmp_path):
    """--no-open prints the link but never touches the browser."""
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--no-open"])
    assert result.exit_code == 0, result.output
    assert launched == []
    assert "api.slack.com/apps?new_app=1" in result.output


def test_cli_default_stays_quiet_when_not_a_tty(monkeypatch, tmp_path):
    """Default (no flag) must not launch a browser under the test runner /
    pipes / CI — only when stdout is an interactive terminal."""
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--app-name", "Bot"])
    assert result.exit_code == 0, result.output
    assert launched == []


def test_cli_interactive_prompts_for_name_and_event_server(monkeypatch, tmp_path):
    """At a real terminal, the command asks for the app name and the event
    server URL (with the localhost-tunnel guidance) BEFORE rendering the
    manifest; typed answers land in it."""
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main, ["create-slack-bot", "--no-open"],
            input="My Bot\nhttps://tunnel.example.com\n",
        )
    assert result.exit_code == 0, result.output
    assert "Slack app display name" in result.output
    assert "localhost:8080" in result.output  # the tunnel guidance
    assert "https://tunnel.example.com/webhooks/slack" in result.output
    assert "name: My Bot" in result.output


def test_cli_interactive_enter_accepts_cloud_default(monkeypatch, tmp_path):
    """Pressing Enter at both prompts uses the default name and the bobi
    cloud event server."""
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main, ["create-slack-bot", "--no-open"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert f"{DEFAULT_EVENT_SERVER}/webhooks/slack" in result.output
    assert "name: bobi agent" in result.output


def test_cli_flags_answer_the_interactive_questions(monkeypatch, tmp_path):
    """--app-name/--event-server pre-answer the prompts: no input is
    consumed even at a real terminal (no input given, so a prompt would
    abort)."""
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--no-open",
            "--app-name", "Bot", "--event-server", EVENT_SERVER,
        ])
    assert result.exit_code == 0, result.output
    assert "Slack app display name" not in result.output
    assert f"{EVENT_SERVER}/webhooks/slack" in result.output
