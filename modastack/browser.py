"""Chromium sandbox detection, fix, and browser health checks for /browse.

gstack's `/browse` skill drives a headless Chromium via Playwright. On
Ubuntu 23.10+ (and other recent kernels) AppArmor restricts unprivileged
user namespaces by default — `kernel.apparmor_restrict_unprivileged_userns`
is set to 1 — which breaks Chromium's sandbox and prevents the browser from
launching at all.

This module centralizes:
  - reading the AppArmor userns sysctl,
  - locating the Playwright Chromium and the browse binary,
  - launching Chromium to detect the sandbox failure,
  - applying (and persisting) the sysctl fix with sudo,
  - the individual health checks behind `modastack doctor`.

The setup flow (`modastack setup`) and the `modastack doctor` command both
build on the functions here.
"""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The AppArmor knob that gates unprivileged user namespaces. 1 = restricted
# (Chromium's sandbox can't initialize), 0 = unrestricted (Chromium works).
USERNS_SYSCTL = "kernel.apparmor_restrict_unprivileged_userns"
USERNS_SYSCTL_PATH = Path("/proc/sys/kernel") / "apparmor_restrict_unprivileged_userns"

# Where the fix is persisted so it survives reboots.
SYSCTL_CONF_PATH = Path("/etc/sysctl.d/99-chromium-sandbox.conf")
SYSCTL_CONF_BODY = f"# Allow Chromium's sandbox to use unprivileged user namespaces (for gstack /browse).\n{USERNS_SYSCTL} = 0\n"

# Playwright stores downloaded browsers here.
PLAYWRIGHT_CACHE = Path.home() / ".cache" / "ms-playwright"

# The compiled gstack browse daemon installed by `gstack setup`.
BROWSE_BINARY = Path.home() / ".claude" / "skills" / "gstack" / "browse" / "dist" / "browse"

# Substrings Chromium prints to stderr when the user-namespace sandbox fails.
SANDBOX_ERROR_MARKERS = (
    "Failed to move to new namespace",
    "clone(): Operation not permitted",
    "No usable sandbox",
    "namespace sandbox",
    "Operation not permitted (1)",
)

FIX_COMMAND = f"sudo sysctl -w {USERNS_SYSCTL}=0"
FIX_HINT = (
    f"Run `{FIX_COMMAND}` (and persist it to {SYSCTL_CONF_PATH}), "
    f"or re-run `modastack setup` to apply it interactively."
)


@dataclass
class CheckResult:
    """Outcome of a single browser health check."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    # Set when the failure is specifically the AppArmor userns sandbox block,
    # so callers (setup) can offer the targeted fix.
    sandbox_error: bool = False


def is_linux() -> bool:
    return platform.system() == "Linux"


def read_userns_restriction() -> int | None:
    """Return the current value of the AppArmor userns sysctl, or None.

    None means the knob doesn't exist on this kernel (older kernels, macOS),
    in which case the restriction simply doesn't apply.
    """
    try:
        return int(USERNS_SYSCTL_PATH.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def userns_restricted() -> bool:
    """True only if the kernel is actively restricting unprivileged userns."""
    return read_userns_restriction() == 1


def looks_like_sandbox_error(text: str) -> bool:
    return any(marker in text for marker in SANDBOX_ERROR_MARKERS)


def _version_key(chromium_dir: Path) -> tuple:
    """Sort key that orders chromium-1223 after chromium-1208."""
    match = re.search(r"(\d+)$", chromium_dir.name)
    return (int(match.group(1)) if match else 0, chromium_dir.name)


def find_chromium_binary() -> Path | None:
    """Locate the newest Playwright Chromium executable, if installed.

    Playwright lays browsers out as
    ``~/.cache/ms-playwright/chromium-<rev>/chrome-linux64/chrome`` (the layout
    varies slightly by platform, so we glob for the executable). Prefers the
    full ``chrome`` build over ``headless_shell`` and the highest revision.
    """
    if not PLAYWRIGHT_CACHE.exists():
        return None

    chromium_dirs = sorted(
        (d for d in PLAYWRIGHT_CACHE.glob("chromium-*") if d.is_dir()),
        key=_version_key,
        reverse=True,
    )
    for chromium_dir in chromium_dirs:
        for pattern in ("chrome-linux*/chrome", "chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
            for candidate in sorted(chromium_dir.glob(pattern)):
                if candidate.exists():
                    return candidate
    return None


def apply_sandbox_fix(persist: bool = True) -> tuple[bool, str]:
    """Disable the AppArmor userns restriction via sudo, optionally persisting.

    Applies the change to the running kernel with ``sudo sysctl -w`` and, when
    ``persist`` is set, writes it to a sysctl.d drop-in so it survives reboots.
    Returns (success, human-readable message).
    """
    runtime = subprocess.run(
        ["sudo", "sysctl", "-w", f"{USERNS_SYSCTL}=0"],
        capture_output=True, text=True,
    )
    if runtime.returncode != 0:
        return False, (runtime.stderr.strip() or "sysctl failed")

    if not persist:
        return True, f"Applied for the current boot ({USERNS_SYSCTL}=0)."

    written = subprocess.run(
        ["sudo", "tee", str(SYSCTL_CONF_PATH)],
        input=SYSCTL_CONF_BODY, capture_output=True, text=True,
    )
    if written.returncode != 0:
        return True, (
            f"Applied for the current boot, but could not persist to "
            f"{SYSCTL_CONF_PATH}: {written.stderr.strip()}"
        )
    return True, f"Applied and persisted to {SYSCTL_CONF_PATH}."


# --- Individual health checks (used by `modastack doctor`) -----------------


def check_playwright_installed() -> CheckResult:
    name = "Playwright Chromium installed"
    if not PLAYWRIGHT_CACHE.exists():
        return CheckResult(
            name, ok=False,
            detail=f"No Playwright cache at {PLAYWRIGHT_CACHE}",
            hint="Install browsers: bunx playwright install chromium",
        )
    chrome = find_chromium_binary()
    if not chrome:
        return CheckResult(
            name, ok=False,
            detail="Playwright cache exists but no Chromium build found",
            hint="Install browsers: bunx playwright install chromium",
        )
    return CheckResult(name, ok=True, detail=str(chrome.parent.parent.name))


def check_chromium_launch(timeout: int = 30) -> CheckResult:
    """Launch Chromium headless (sandbox enabled) and confirm it starts.

    Deliberately does NOT pass ``--no-sandbox`` — the point is to exercise the
    real sandbox so we surface the AppArmor userns failure when present.
    """
    name = "Chromium launches"
    chrome = find_chromium_binary()
    if not chrome:
        return CheckResult(
            name, ok=False,
            detail="Chromium not found in Playwright cache",
            hint="Install browsers: bunx playwright install chromium",
        )

    try:
        proc = subprocess.run(
            [str(chrome), "--headless=new", "--no-startup-window",
             "--disable-gpu", "--dump-dom", "about:blank"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name, ok=False, detail=f"Chromium launch timed out after {timeout}s")
    except OSError as e:
        return CheckResult(name, ok=False, detail=f"Could not exec Chromium: {e}")

    combined = (proc.stdout or "") + (proc.stderr or "")
    if looks_like_sandbox_error(combined):
        return CheckResult(
            name, ok=False, sandbox_error=True,
            detail="Chromium sandbox blocked — AppArmor restricts unprivileged user namespaces",
            hint=FIX_HINT,
        )
    if proc.returncode != 0:
        snippet = (proc.stderr or combined).strip().splitlines()
        tail = snippet[-1] if snippet else f"exit {proc.returncode}"
        return CheckResult(name, ok=False, detail=f"Chromium exited {proc.returncode}: {tail[:200]}")
    return CheckResult(name, ok=True, detail="headless launch succeeded")


def check_userns_sandbox() -> CheckResult:
    """Report the AppArmor userns sysctl state directly."""
    name = "AppArmor userns sandbox"
    value = read_userns_restriction()
    if value is None:
        return CheckResult(name, ok=True, detail="kernel does not restrict unprivileged userns")
    if value == 0:
        return CheckResult(name, ok=True, detail=f"{USERNS_SYSCTL}=0 (unrestricted)")
    return CheckResult(
        name, ok=False, sandbox_error=True,
        detail=f"{USERNS_SYSCTL}=1 — Chromium's sandbox cannot initialize",
        hint=FIX_HINT,
    )


def check_browse_daemon(timeout: int = 60) -> CheckResult:
    """Start the gstack browse daemon, load a local page, and snapshot it.

    Uses a temporary ``file:`` URL rather than the network so the check works
    offline — browse only permits http/https/file URLs, so a local HTML file
    is the simplest self-contained target.
    """
    import tempfile

    name = "Browse daemon"
    if not BROWSE_BINARY.exists():
        return CheckResult(
            name, ok=False,
            detail=f"browse binary not found at {BROWSE_BINARY}",
            hint="Install gstack skills (see deploy/INSTALL.md step 4)",
        )

    def _browse(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(BROWSE_BINARY), *args],
            capture_output=True, text=True, timeout=timeout,
        )

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", prefix="modastack-doctor-", delete=False
        ) as fh:
            fh.write("<!doctype html><title>modastack doctor</title>"
                     "<h1>browse ok</h1>")
            tmp = Path(fh.name)

        goto = _browse("goto", tmp.as_uri())
        if goto.returncode != 0:
            tail = (goto.stderr or goto.stdout).strip().splitlines()
            return CheckResult(name, ok=False,
                               detail=f"browse goto failed: {(tail[-1] if tail else '')[:200]}")
        snap = _browse("snapshot")
        if snap.returncode != 0:
            tail = (snap.stderr or snap.stdout).strip().splitlines()
            return CheckResult(name, ok=False,
                               detail=f"browse snapshot failed: {(tail[-1] if tail else '')[:200]}")
        return CheckResult(name, ok=True, detail="daemon started and captured a snapshot")
    except subprocess.TimeoutExpired:
        return CheckResult(name, ok=False, detail=f"browse timed out after {timeout}s")
    except OSError as e:
        return CheckResult(name, ok=False, detail=f"could not run browse: {e}")
    finally:
        try:
            _browse("stop")
        except (subprocess.SubprocessError, OSError):
            pass
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def check_system_deps() -> CheckResult:
    """Confirm Chromium's shared libraries resolve (libx11, libnss3, ...)."""
    name = "System dependencies"
    chrome = find_chromium_binary()
    if not chrome:
        return CheckResult(name, ok=False, detail="Chromium not installed — cannot check libraries")

    try:
        ldd = subprocess.run(["ldd", str(chrome)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(name, ok=True, detail=f"skipped (ldd unavailable: {e})")

    missing = sorted({
        line.split("=>")[0].strip()
        for line in ldd.stdout.splitlines()
        if "not found" in line
    })
    if missing:
        return CheckResult(
            name, ok=False,
            detail=f"missing shared libraries: {', '.join(missing)}",
            hint="Install browser deps: bunx playwright install-deps chromium "
                 "(or sudo apt install -y libnss3 libx11-6 libxcomposite1 libxdamage1)",
        )
    return CheckResult(name, ok=True, detail="all Chromium libraries resolve")


def run_doctor() -> list[CheckResult]:
    """Run the full browser health check suite in order."""
    results = [
        check_playwright_installed(),
        check_userns_sandbox(),
        check_chromium_launch(),
        check_system_deps(),
        check_browse_daemon(),
    ]
    return results
