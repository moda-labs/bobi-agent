"""Tests for the venn CLI subprocess wrapper used in setup discovery."""

from unittest.mock import patch, MagicMock

from modastack.setup.venn_cli import run_venn, MAX_OUTPUT_CHARS


def _completed(stdout="", stderr="", code=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = code
    return proc


class TestRefusals:
    @patch("modastack.setup.venn_cli.venn_binary", return_value=None)
    def test_missing_binary_suggests_fallback(self, _):
        result = run_venn("tools search 'list emails'", "key")
        assert not result.ok
        assert "description-only" in result.refused

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_confirm_flag_refused(self, _):
        result = run_venn(
            "tools execute -s gmail -t send_email -a '{}' --confirm", "key")
        assert not result.ok
        assert "read-only" in result.refused

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_confirm_equals_variant_refused(self, _):
        result = run_venn(
            "tools execute -s gmail -t send_email -a '{}' --confirm=true", "key")
        assert not result.ok
        assert "read-only" in result.refused

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_api_key_arg_refused(self, _):
        result = run_venn("tools search x --api-key=abc", "key")
        assert not result.ok
        assert "environment" in result.refused

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    def test_unparseable_args_refused(self, _):
        result = run_venn("tools search 'unclosed", "key")
        assert not result.ok
        assert "unparseable" in result.refused


class TestExecution:
    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("modastack.setup.venn_cli.subprocess.run")
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

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("modastack.setup.venn_cli.subprocess.run")
    def test_leading_venn_token_stripped(self, mock_run, _):
        mock_run.return_value = _completed(stdout="ok")
        run_venn("venn help list_servers", "key")
        argv = mock_run.call_args.args[0]
        assert argv == ["/usr/bin/venn", "help", "list_servers"]

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("modastack.setup.venn_cli.subprocess.run")
    def test_failure_includes_stderr(self, mock_run, _):
        mock_run.return_value = _completed(stdout="", stderr="no such tool", code=1)
        result = run_venn("tools describe -s x -t y", "key")
        assert not result.ok
        assert "no such tool" in result.output

    @patch("modastack.setup.venn_cli.venn_binary", return_value="/usr/bin/venn")
    @patch("modastack.setup.venn_cli.subprocess.run")
    def test_output_truncated(self, mock_run, _):
        mock_run.return_value = _completed(stdout="x" * (MAX_OUTPUT_CHARS + 500))
        result = run_venn("tools search big", "key")
        assert "truncated" in result.output
        assert len(result.output) < MAX_OUTPUT_CHARS + 100
