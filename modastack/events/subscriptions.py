"""Auto-discover event subscriptions from the environment."""

import json
import logging
import subprocess
import urllib.request
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def discover_subscriptions(project_path: Path, agent_name: str | None = None) -> list[str]:
    """Build subscription keys by auto-detecting event sources.

    Resolution order:
    1. agent.yaml subscribe list (explicit override)
    2. agent.yaml services with events: true (native services auto-detected)
    3. Fallback to project directory name
    """
    agent_yaml = project_path / ".modastack" / "agent.yaml"
    if agent_yaml.exists():
        try:
            raw = yaml.safe_load(agent_yaml.read_text()) or {}
            explicit = raw.get("subscribe", [])
            if explicit:
                return list(explicit)
        except Exception:
            pass

    from modastack.config import Config
    cfg = Config.load(project_path, agent_name=agent_name)
    if cfg.event_services:
        subs = []
        for svc in cfg.event_services:
            if svc.name in cfg.native_services:
                keys = _resolve_source(svc.name, project_path)
                subs.extend(keys)
        if subs:
            return subs

    return [project_path.name]


def _resolve_source(source: str, project_path: Path) -> list[str]:
    """Resolve a source name to concrete subscription keys."""
    if source == "github":
        return _detect_github(project_path)
    elif source == "slack":
        return _detect_slack(project_path)
    elif source == "linear":
        return _detect_linear(project_path)
    else:
        return [source]


def _detect_github(project_path: Path) -> list[str]:
    """Detect github:org/repo from git remote."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            cwd=str(project_path),
        )
        if result.returncode != 0:
            return []
        url = result.stdout.strip()
        slug = _parse_github_url(url)
        if slug:
            log.info(f"Auto-detected GitHub repo: {slug}")
            return [f"github:{slug}"]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def _parse_github_url(url: str) -> str:
    """Extract org/repo from a GitHub remote URL."""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com" in url:
        parts = url.split("github.com")[-1].lstrip(":/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return ""


def _detect_slack(project_path: Path) -> list[str]:
    """Detect slack:WORKSPACE_ID from the bot token via auth.test."""
    from modastack.config import Config
    cfg = Config.load(project_path)
    if not cfg.slack_bot_token:
        return []
    try:
        req = urllib.request.Request(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {cfg.slack_bot_token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get("ok") and data.get("team_id"):
            log.info(f"Auto-detected Slack workspace: {data['team_id']}")
            return [f"slack:{data['team_id']}"]
    except Exception as e:
        log.debug(f"Slack auto-detection failed: {e}")
    return []


def _detect_linear(project_path: Path) -> list[str]:
    """Detect linear:TEAM from the Linear API."""
    from modastack.config import Config
    cfg = Config.load(project_path)
    if not cfg.linear_api_key:
        return []
    try:
        payload = json.dumps({
            "query": "{ teams { nodes { key } } }"
        }).encode()
        req = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=payload,
            headers={
                "Authorization": cfg.linear_api_key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        teams = data.get("data", {}).get("teams", {}).get("nodes", [])
        keys = [f"linear:{t['key']}" for t in teams if t.get("key")]
        if keys:
            log.info(f"Auto-detected Linear teams: {keys}")
        return keys
    except Exception as e:
        log.debug(f"Linear auto-detection failed: {e}")
    return []
