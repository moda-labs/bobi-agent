"""Pack prompts must only reference modastack CLI commands that exist.

Tool guides and role prompts teach agents CLI invocations. A guide that
documents a nonexistent command ships agents that try to run it — this
drift class reached main twice (`modastack slack-send`, a fictional
`modastack linear` group). Mechanics belong to the CLI's own surfaces
(--help, `modastack skill`); guides may name commands but every name
must be real.
"""

import re
from pathlib import Path

import pytest

from modastack.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent

# modastack invocations inside fenced code blocks or inline code spans.
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE = re.compile(r"`[^`\n]+`")
_INVOCATION = re.compile(r"\bmodastack\s+([a-z][a-z0-9-]*)")


def _prompt_files() -> list[Path]:
    files = [REPO_ROOT / "modastack" / "prompts" / "base.md"]
    agents_dir = REPO_ROOT / "agents"
    for pack in sorted(agents_dir.iterdir()):
        if pack.is_dir():
            files += sorted(pack.glob("agent.md"))
            files += sorted(pack.glob("tools/*.md"))
            files += sorted(pack.glob("roles/*/ROLE.md"))
            files += sorted(pack.glob("workflows/*.yaml"))
            files += sorted(pack.glob("monitors/*.yaml"))
    return [f for f in files if f.exists()]


def _referenced_commands(text: str) -> set[str]:
    code = "\n".join(_FENCE.findall(text))
    # Strip fences before scanning inline spans so blocks aren't re-matched.
    code += "\n" + "\n".join(_INLINE.findall(_FENCE.sub("", text)))
    return set(_INVOCATION.findall(code))


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_prompt_references_real_cli_commands(path):
    real = set(cli_main.commands.keys())
    referenced = _referenced_commands(path.read_text())
    unknown = sorted(referenced - real)
    assert not unknown, (
        f"{path.relative_to(REPO_ROOT)} references nonexistent modastack "
        f"command(s): {unknown}. Available: {sorted(real)}"
    )
