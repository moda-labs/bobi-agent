"""Tests for the venn CLI subprocess wrapper used in setup discovery."""

from unittest.mock import patch, MagicMock

from bobi.setup.venn_cli import run_venn, MAX_OUTPUT_CHARS


def _completed(stdout="", stderr="", code=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = code
    return proc


class TestRefusals:
    @patch("bobi.setup.venn_cli.venn_binary", return_value=None)
    def test_missing_binary_suggests_fallback(self, _):
        result = run_venn("tools search 'list emails'", "key")
        assert not result.ok
        assert "description-only" in result.refused

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_confirm_flag_refused(self, _):
        result = run_venn(
            "tools execute -s gmail -t send_email -a '{}' --confirm", "key")
        assert not result.ok
        assert "read-only" in result.refused

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_confirm_equals_variant_refused(self, _):
        result = run_venn(
            "tools execute -s gmail -t send_email -a '{}' --confirm=true", "key")
        assert not result.ok
        assert "read-only" in result.refused

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_api_key_arg_refused(self, _):
        result = run_venn("tools search x --api-key=abc", "key")
        assert not result.ok
        assert "environment" in result.refused

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_unparseable_args_refused(self, _):
        result = run_venn("tools search 'unclosed", "key")
        assert not result.ok
        assert "unparseable" in result.refused


class TestExecution:
    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("bobi.setup.venn_cli.subprocess.run")
    def test_key_injected_into_env_not_args(self, mock_run, _):
        mock_run.return_value = _completed(stdout='[{"id": "m1"}]')

        result = run_venn(
            'tools execute -s work-gmail -t list_messages -a \'{"maxResults": 5}\'',
            "secret-key")

        assert result.ok
        assert result.output == '[{"id": "m1"}]'
        argv = mock_run.call_args.args[0]
        env = mock_run.call_args.kwargs["env"]
        assert env["VENN_API_KEY"] == "secret-key"
        assert "secret-key" not in " ".join(argv)
        assert argv[:4] == ["/usr/bin/venn", "tools", "execute", "-s"]

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("bobi.setup.venn_cli.subprocess.run")
    def test_leading_venn_token_stripped(self, mock_run, _):
        mock_run.return_value = _completed(stdout="ok")
        run_venn("venn help list_servers", "key")
        argv = mock_run.call_args.args[0]
        assert argv == ["/usr/bin/venn", "help", "list_servers"]

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("bobi.setup.venn_cli.subprocess.run")
    def test_failure_includes_stderr(self, mock_run, _):
        mock_run.return_value = _completed(stdout="", stderr="no such tool", code=1)
        result = run_venn("tools describe -s x -t y", "key")
        assert not result.ok
        assert "no such tool" in result.output

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("bobi.setup.venn_cli.subprocess.run")
    def test_output_truncated(self, mock_run, _):
        mock_run.return_value = _completed(stdout="x" * (MAX_OUTPUT_CHARS + 500))
        result = run_venn("tools search big", "key")
        assert "truncated" in result.output
        assert len(result.output) < MAX_OUTPUT_CHARS + 100


class TestListServers:
    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("bobi.setup.venn_cli.subprocess.run")
    def test_parses_servers_and_names(self, mock_run, _):
        import json as _json
        from bobi.setup.venn_cli import list_servers, list_service_names
        mock_run.return_value = _completed(stdout=_json.dumps({"result": {"servers": [
            {"server_id": "s1", "server_name": "gmail", "connected": True},
            {"server_id": "s2", "server_name": "PostHog", "connected": False},
        ]}}))
        servers = list_servers("key")
        assert {s["name"] for s in servers} == {"gmail", "PostHog"}
        assert next(s for s in servers if s["name"] == "gmail")["connected"] is True
        # all names (connected or not) form the catalog, lowercased
        assert list_service_names("key") == {"gmail", "posthog"}
        # the global --json flag precedes the subcommand
        argv = mock_run.call_args.args[0]
        assert argv[:4] == ["/usr/bin/venn", "--json", "help", "list_servers"]

    @patch("bobi.setup.venn_cli.venn_binary", return_value=None)
    def test_missing_binary_returns_empty(self, _):
        from bobi.setup.venn_cli import list_servers
        assert list_servers("key") == []

    @patch("bobi.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("bobi.setup.venn_cli.subprocess.run")
    def test_unparseable_output_returns_empty(self, mock_run, _):
        from bobi.setup.venn_cli import list_servers
        mock_run.return_value = _completed(stdout="not json at all")
        assert list_servers("key") == []
