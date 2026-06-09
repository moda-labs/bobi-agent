"""Auto-generate .modastack/agent.yaml by inspecting a project."""

import json
import re
from pathlib import Path

import yaml


def detect_test_command(project_path: Path) -> str:
    """Infer the test command from project files."""
    # Python
    if (project_path / "pyproject.toml").exists():
        return "pytest"
    if (project_path / "setup.py").exists():
        return "pytest"
    if (project_path / "tox.ini").exists():
        return "tox"

    # JavaScript/TypeScript
    pkg_json = project_path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                # Use the package manager that matches the lockfile
                runner = detect_package_manager(project_path)
                return f"{runner} test"
        except (json.JSONDecodeError, KeyError):
            pass

    # Go
    if (project_path / "go.mod").exists():
        return "go test ./..."

    # Rust
    if (project_path / "Cargo.toml").exists():
        return "cargo test"

    # Ruby
    if (project_path / "Gemfile").exists():
        return "bundle exec rspec"

    # Makefile
    makefile = project_path / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if "test:" in content:
            return "make test"

    return ""


def detect_package_manager(project_path: Path) -> str:
    """Detect JS package manager from lockfile."""
    if (project_path / "bun.lockb").exists() or (project_path / "bun.lock").exists():
        return "bun"
    if (project_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_path / "yarn.lock").exists():
        return "yarn"
    return "npm"


def detect_linear_project(project_path: Path) -> str:
    """Try to infer Linear project key from repo name or branch conventions."""
    # Check for existing Linear references in CLAUDE.md or README
    for candidate in ["CLAUDE.md", "README.md", ".github/ISSUE_TEMPLATE"]:
        f = project_path / candidate
        if f.exists() and f.is_file():
            content = f.read_text()
            # Look for Linear project key patterns like "PROJ-123"
            match = re.search(r"\b([A-Z]{2,10})-\d+", content)
            if match:
                return match.group(1)

    # Fall back to repo directory name uppercased as a guess
    return project_path.name.upper().replace("-", "")[:6]


def detect_skills(project_path: Path) -> list[str]:
    """Suggest gstack skills based on repo contents."""
    skills = ["review", "ship"]

    # If there's a web frontend, suggest QA
    if any((project_path / f).exists() for f in [
        "index.html", "src/App.tsx", "src/App.vue",
        "src/App.svelte", "app/page.tsx", "templates/",
    ]):
        skills.append("qa")

    # If there's deployment config, suggest land-and-deploy
    if any((project_path / f).exists() for f in [
        "fly.toml", "render.yaml", "vercel.json",
        "netlify.toml", "Procfile", "Dockerfile",
    ]):
        skills.append("land-and-deploy")

    return skills


def generate_dispatch_yaml(project_path: Path, task_tracking: str = "github-issues") -> dict:
    """Generate a .modastack/config.yaml for the project."""
    test_cmd = detect_test_command(project_path)
    project = detect_linear_project(project_path)
    skills = detect_skills(project_path)

    return {
        "task_tracking": {
            "system": task_tracking,
            "project": project,
            "trigger_labels": ["agent"],
            "skip_labels": ["blocked", "human-only"],
        },
        "complexity": {
            "trivial": "label:typo OR label:docs OR label:config",
            "medium": "default",
            "heavy": "label:feature OR label:refactor OR estimate>3",
        },
        "agent": {
            "tool": "claude",
            "skills": skills,
            "max_parallel": 2,
        },
        "verify": {
            "test_command": test_cmd,
            "review_required": True,
            "auto_merge": False,
        },
    }


def setup_project(project_path: Path) -> Path:
    """Generate .modastack/agent.yaml and return the path."""
    config = generate_dispatch_yaml(project_path)
    output_dir = project_path / ".modastack"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "agent.yaml"
    output_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    return output_path
