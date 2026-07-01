"""Host capabilities declared by a dependency's `host:` field (#428 Stage 3).

Some dependencies need a capability an in-container agent cannot grant itself —
a kernel sysctl, a device — because it belongs to the host / the container
runtime, not the image. `/browse` (gstack's Chromium sandbox) is the motivating
case: it needs `kernel.apparmor_restrict_unprivileged_userns=0`, which no bake
step can set. This module generalizes what `bobi/browser.py` hard-coded for that
one sysctl into the declarative `host:` field, so ANY dependency can declare a
host capability and get it verified (doctor) and surfaced (deploy) uniformly.

`host:` is **runtime wiring**: the snapshot never holds it and the in-container
bootstrap never attempts it (see `tool_library._expand_dependency`). Deploy
surfaces the requirement to the operator; the runtime doctor verifies it.

Schema (a dependency's `host:` list):

    host:
      - sysctl: kernel.apparmor_restrict_unprivileged_userns=0

Each item is a mapping naming one capability kind (`sysctl` today) whose value
carries its parameter. Unknown kinds are ignored with a warning rather than
failing a load, so an older framework tolerates a newer capability it can't check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from bobi.doctor import CheckResult

log = logging.getLogger(__name__)

# A sysctl's runtime view lives under /proc/sys with dots as path separators.
PROC_SYS = Path("/proc/sys")
# Where a persisted sysctl fix is written so it survives reboots.
SYSCTL_CONF_DIR = Path("/etc/sysctl.d")


@dataclass(frozen=True)
class HostCap:
    """One host capability a dependency requires (kind + parameter).

    `sysctl` is the only kind today: `key` is the dotted sysctl name and `value`
    the required setting. `owner` names the declaring dependency for messaging.
    """

    kind: str
    key: str
    value: str
    owner: str = ""

    @staticmethod
    def sysctl(key: str, value: str, owner: str = "") -> "HostCap":
        return HostCap(kind="sysctl", key=key, value=value, owner=owner)

    @property
    def spec(self) -> str:
        """The `key=value` form (what a `host:` entry declares / a fix applies)."""
        return f"{self.key}={self.value}"

    @property
    def proc_path(self) -> Path:
        """The /proc/sys path exposing this sysctl's current value."""
        return PROC_SYS / self.key.replace(".", "/")

    @property
    def conf_path(self) -> Path:
        """The sysctl.d drop-in that persists this setting across reboots."""
        stem = self.key.replace(".", "-")
        return SYSCTL_CONF_DIR / f"99-bobi-{stem}.conf"

    def fix_command(self) -> str:
        """The command an operator runs to apply this capability on the host."""
        if self.kind == "sysctl":
            return f"sudo sysctl -w {self.spec}"
        return f"# no known fix for host capability kind '{self.kind}'"

    def read(self) -> str | None:
        """Current value on this host, or None if the sysctl doesn't exist here
        (an older kernel / non-Linux, where the restriction simply doesn't apply)."""
        if self.kind != "sysctl":
            return None
        try:
            return self.proc_path.read_text().strip()
        except (FileNotFoundError, OSError):
            return None

    def satisfied(self) -> bool | None:
        """True/False if verifiable here, None if not applicable on this host."""
        if self.kind != "sysctl":
            return None
        current = self.read()
        if current is None:
            return None  # knob absent → capability not needed on this host
        return current == self.value

    def check(self) -> CheckResult:
        """A doctor CheckResult for this capability."""
        label = f"Host cap: {self.key}" + (f" ({self.owner})" if self.owner else "")
        state = self.satisfied()
        if state is None:
            return CheckResult(label, ok=True,
                               detail=f"{self.key} not present on this host — "
                                      f"capability not required here")
        if state:
            return CheckResult(label, ok=True, detail=f"{self.spec} (satisfied)")
        current = self.read()
        return CheckResult(
            label, ok=False,
            detail=f"{self.key}={current} — required {self.value}",
            hint=f"Provision on the host: `{self.fix_command()}` "
                 f"(persist to {self.conf_path}). The in-container agent cannot "
                 f"set this.")


def parse_host_caps(entries: list, *, owner: str = "") -> list[HostCap]:
    """Parse a dependency's `host:` list into HostCap objects.

    Each entry is a mapping naming one capability kind. `sysctl: key=value` is the
    only kind today; a value without `=` or an unknown kind is skipped with a
    warning (forward-compatibility: an old framework tolerates a new capability).
    """
    caps: list[HostCap] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            log.warning("ignoring malformed host: entry (not a mapping): %r", entry)
            continue
        if "sysctl" in entry:
            spec = str(entry["sysctl"])
            if "=" not in spec:
                log.warning("ignoring host sysctl without '=' (need key=value): %r",
                            spec)
                continue
            key, value = spec.split("=", 1)
            caps.append(HostCap.sysctl(key.strip(), value.strip(), owner=owner))
        else:
            log.warning("ignoring host: entry with no known capability kind: %r",
                        entry)
    return caps


def host_caps_for_deps(deps: list) -> list[HostCap]:
    """Collect the host capabilities declared across a list of Dependency objects.

    De-duped by (kind, key, value) so the same capability required by two
    dependencies surfaces once."""
    caps: list[HostCap] = []
    seen: set[tuple[str, str, str]] = set()
    for dep in deps:
        for cap in parse_host_caps(getattr(dep, "host", []), owner=dep.name):
            marker = (cap.kind, cap.key, cap.value)
            if marker in seen:
                continue
            seen.add(marker)
            caps.append(cap)
    return caps


def host_caps_for_team(team_dir: Path, project_path: Path | None = None) -> list[HostCap]:
    """Resolve a team's full declared dependency set and collect its host caps."""
    from bobi.build_render import _workspace_root
    from bobi.tool_library import resolve_team_dependencies

    project_path = project_path or _workspace_root(team_dir)
    deps = resolve_team_dependencies(team_dir, project_path)
    return host_caps_for_deps(deps)


def describe_for_deploy(caps: list[HostCap]) -> str:
    """A one-block operator-facing summary of host caps a deploy must provision.

    Empty string when there are none, so the caller can skip the notice.
    """
    if not caps:
        return ""
    lines = ["This team's dependencies require host capabilities the container "
             "cannot grant itself; ensure they are set on the deploy host:"]
    for cap in caps:
        owner = f" (for {cap.owner})" if cap.owner else ""
        lines.append(f"  - {cap.spec}{owner}: `{cap.fix_command()}`")
    return "\n".join(lines)
