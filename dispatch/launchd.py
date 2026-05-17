"""Install/uninstall a launchd agent instead of cron.

launchd agents run in the user's login session, which means they can
access the macOS Keychain (required for Claude's OAuth). cron jobs
cannot access the Keychain — this is a macOS security restriction.
"""

import os
import sys
import subprocess
from pathlib import Path

PLIST_NAME = "com.agent-dispatch.cycle"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"


def get_plist_path() -> Path:
    return PLIST_DIR / f"{PLIST_NAME}.plist"


def get_plist_content() -> str:
    dispatch_bin = Path(sys.executable).parent / "dispatch"
    log_dir = Path.home() / ".dispatch"
    log_dir.mkdir(parents=True, exist_ok=True)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{dispatch_bin}</string>
        <string>cycle</string>
    </array>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/dispatch.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/dispatch.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{Path.home()}</string>
        <key>PATH</key>
        <string>{os.environ.get("PATH", "/usr/bin:/bin")}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def install() -> str:
    """Install the launchd agent. Returns status message."""
    plist_path = get_plist_path()
    PLIST_DIR.mkdir(parents=True, exist_ok=True)

    # Unload if already loaded
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    plist_path.write_text(get_plist_content())
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)

    if result.returncode != 0:
        return f"Failed to load: {result.stderr}"

    return f"Installed at {plist_path}. Runs every 60 seconds."


def uninstall() -> str:
    """Uninstall the launchd agent."""
    plist_path = get_plist_path()
    if not plist_path.exists():
        return "Not installed."

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink()
    return "Uninstalled."


def status() -> str:
    """Check if the launchd agent is running."""
    result = subprocess.run(
        ["launchctl", "list", PLIST_NAME],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return f"Running.\n{result.stdout.strip()}"
    return "Not running."
