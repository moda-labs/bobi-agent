import pytest

from bobi.metrics.features import build_features, prepare_task
from bobi.redact import redact_secrets
from bobi.setup.actions import redact_secrets as setup_redact

def test_shared_redaction_preserves_setup_import():
    assert setup_redact is redact_secrets
    assert redact_secrets("API_KEY=private-value")[0] == "API_KEY=[redacted]"
    assert redact_secrets("sk-abcdefghijklmnopqrstu-other-private")[0] == "[redacted]"

def test_task_strips_memory_and_redacts_paths_and_credentials():
    prompt = ("Fix /repo/src/main.py using API_KEY=private-value. "
              "Ignore /other/private.txt and C:\\private\\secret.txt\n"
              "## Long-Term Memory\n## Facts\nprivate memory\n## Decisions\nprivate decision")
    task, count = prepare_task(prompt, repo_path="/repo", max_bytes=8192)
    assert "src/main.py" in task
    assert "private-value" not in task and "private memory" not in task
    assert "/other" not in task and "C:\\private" not in task
    assert count == 3

@pytest.mark.parametrize("limit", [1, 2, 4, 5, 6, 11, 20])
def test_utf8_head_tail_respects_byte_cap(limit):
    task, _ = prepare_task("你好" * 30, repo_path="/repo", max_bytes=limit)
    assert len(task.encode("utf-8")) <= limit
    assert "�" not in task

def test_features_none_never_contains_prompt_and_invalid_identifiers_fail():
    args = dict(prompt="private prompt", repo_path="/repo", entry_point="session_start",
                role="engineer", brain="codex", prompt_egress="none")
    features, count = build_features(**args)
    assert "task" not in features and count == 0
    assert features["prompt_bytes"] == 14
    with pytest.raises(ValueError):
        build_features(**{**args, "role": "/private/path"})
    with pytest.raises(ValueError):
        build_features(**{**args, "prompt_egress": "default"})


@pytest.mark.parametrize("root,prompt,expected", [
    ("/repo", 'Read "/repo/source files/main.py"', 'Read "source files/main.py"'),
    ("/repo", "Read '/private/customer files/key.txt'", "Read '[path]'"),
    ("/repo", r"Read \\private-server\customer\key.txt", "Read [path]"),
    (r"C:\repo", r'Read "C:\repo\source files\main.py"', 'Read "source files/main.py"'),
    (r"\\server\repo", r"Read \\server\repo\main.py", "Read main.py"),
    ("/repo", 'Read "/repo/../private/customer files/key.txt"', 'Read "[path]"'),
])
def test_quoted_and_windows_paths_do_not_disclose_external_locations(root, prompt, expected):
    task, _ = prepare_task(prompt, repo_path=root, max_bytes=8192)
    assert task == expected
