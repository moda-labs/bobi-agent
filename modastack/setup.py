"""Repo setup utilities — skill installation, Linear bootstrap, auto-detection."""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent


def detect_test_command(repo_path: Path) -> str:
    """Infer the test command from project files."""
    if (repo_path / "pyproject.toml").exists():
        return "pytest"
    if (repo_path / "setup.py").exists():
        return "pytest"
    if (repo_path / "tox.ini").exists():
        return "tox"

    pkg_json = repo_path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                runner = detect_package_manager(repo_path)
                return f"{runner} test"
        except (json.JSONDecodeError, KeyError):
            pass

    if (repo_path / "go.mod").exists():
        return "go test ./..."
    if (repo_path / "Cargo.toml").exists():
        return "cargo test"
    if (repo_path / "Gemfile").exists():
        return "bundle exec rspec"

    makefile = repo_path / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if "test:" in content:
            return "make test"

    return ""


def detect_package_manager(repo_path: Path) -> str:
    """Detect JS package manager from lockfile."""
    if (repo_path / "bun.lockb").exists() or (repo_path / "bun.lock").exists():
        return "bun"
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_path / "yarn.lock").exists():
        return "yarn"
    return "npm"


def detect_linear_project(repo_path: Path) -> str:
    """Try to infer Linear project key from repo name or branch conventions."""
    for candidate in ["CLAUDE.md", "README.md", ".github/ISSUE_TEMPLATE"]:
        f = repo_path / candidate
        if f.exists() and f.is_file():
            content = f.read_text()
            match = re.search(r"\b([A-Z]{2,10})-\d+", content)
            if match:
                return match.group(1)

    return repo_path.name.upper().replace("-", "")[:6]


def detect_skills(repo_path: Path) -> list[str]:
    """Suggest gstack skills based on repo contents."""
    skills = ["review", "ship"]

    if any((repo_path / f).exists() for f in [
        "index.html", "src/App.tsx", "src/App.vue",
        "src/App.svelte", "app/page.tsx", "templates/",
    ]):
        skills.append("qa")

    if any((repo_path / f).exists() for f in [
        "fly.toml", "render.yaml", "vercel.json",
        "netlify.toml", "Procfile", "Dockerfile",
    ]):
        skills.append("land-and-deploy")

    return skills


def install_skill_symlinks(repo_path: Path) -> list[str]:
    """Install engineer skills + shared tools as symlinks in .claude/skills/."""
    target_skills = repo_path / ".claude" / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    installed = []

    skill_dirs = [
        REPO_ROOT / "engineer" / "process",
        REPO_ROOT / "engineer" / "practices",
        REPO_ROOT / "tools",
    ]
    for category_dir in skill_dirs:
        if not category_dir.exists():
            continue
        for skill_dir in category_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                link = target_skills / skill_dir.name
                if link.exists() or link.is_symlink():
                    continue
                link.symlink_to(skill_dir.resolve())
                installed.append(skill_dir.name)

    return sorted(installed)


def full_setup(repo_path: Path, credential_name: str = "") -> None:
    """Install skills and bootstrap Linear board for a repo."""
    installed = install_skill_symlinks(repo_path)
    if installed:
        for name in installed:
            log.info(f"Linked /{name}")

    if credential_name:
        from .config import Credentials
        creds = Credentials.load()
        cred_data = creds.get(credential_name)
        api_key = cred_data.get("linear_api_key")
        if api_key:
            linear_project = detect_linear_project(repo_path)
            if linear_project:
                log.info("Bootstrapping Linear board...")
                from .board_setup import bootstrap_board
                for action in bootstrap_board(api_key, linear_project):
                    log.info(f"  {action}")
