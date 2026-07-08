"""Tests for the model-passthrough instruction injected into persistent manager
prompts (adhoc-b9088f9c): the manager should pass a user's requested model
through to `subagents launch --model` rather than dropping the request.
"""

from bobi import paths


def _install_role(tmp_path, role="director"):
    roles_dir = paths.roles_dir(tmp_path) / role
    roles_dir.mkdir(parents=True)
    (roles_dir / "ROLE.md").write_text(f"# {role.title()}\nYou direct things.")


class TestModelPassthroughInjection:
    def test_build_startup_prompt_includes_passthrough_note(self, tmp_path):
        from bobi.prompts.resolver import build_startup_prompt
        _install_role(tmp_path)
        result = build_startup_prompt("director", tmp_path, agent_name="test")
        assert "Passing Through Model Requests" in result
        assert "--model <alias>" in result
        assert "subagents launch" in result

    def test_resolve_agent_prompt_omits_startup_only_note(self, tmp_path):
        # Worker prompts are built via resolve_agent_prompt directly, not
        # build_startup_prompt, and shouldn't carry manager-only instructions.
        from bobi.prompts.resolver import resolve_agent_prompt
        _install_role(tmp_path, role="engineer")
        result = resolve_agent_prompt("engineer", tmp_path)
        assert "Passing Through Model Requests" not in result
