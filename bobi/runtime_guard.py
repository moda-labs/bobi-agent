"""Runtime filesystem write policy for Bobi-owned install roots."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.metadata as metadata
import json
import logging
import math
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

from bobi import fsutil, paths

logger = logging.getLogger(__name__)

ProtectedKind = Literal[
    "team-package",
    "bobi-package",
    "bobi-dist-info",
    "venv",
    "dependency",
]

FRAMEWORK_KINDS = frozenset({"bobi-package", "bobi-dist-info"})
RELEASE_WINDOW = 15 * 60
RELEASE_CLOCK_SKEW = 60
RELEASE_MARKER = "runtime-guard-released"


@dataclass(frozen=True)
class ProtectedRoot:
    path: Path
    kind: ProtectedKind
    mode: Literal["readonly"] = "readonly"
    reason: str = ""


@dataclass
class GuardReport:
    protected: list[ProtectedRoot] = field(default_factory=list)
    released: list[ProtectedRoot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReleaseWindowStatus:
    marker_path: Path
    state: Literal["missing", "opening", "open", "expired", "invalid"]
    detail: str
    prefix: Path | None = None
    expires_at: float | None = None
    opened_by: str = ""
    pid: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def covers(self, root: Path) -> bool:
        return bool(
            self.is_open
            and self.prefix is not None
            and _is_relative_to(root, self.prefix)
        )


@dataclass
class ReleaseReport:
    roots: list[ProtectedRoot] = field(default_factory=list)
    released: list[ProtectedRoot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    marker_path: Path | None = None
    prefix: Path | None = None
    expires_at: float | None = None
    opened_by: str = ""
    noop_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped


@dataclass
class ReapplyReport:
    guard: GuardReport
    marker_path: Path
    marker_error: str = ""


@dataclass
class PolicyCheck:
    ok: bool
    detail: str = ""
    protected: list[ProtectedRoot] = field(default_factory=list)
    released: list[ProtectedRoot] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    window: ReleaseWindowStatus | None = None


def _writable_bits(mode: int) -> int:
    return mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def _readonly_mode(mode: int) -> int:
    return mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def _mutable_mode(mode: int) -> int:
    return mode | stat.S_IWUSR


def _chmod_tree(root: Path, mode_fn, *, strict: bool = False) -> list[str]:
    skipped: list[str] = []
    if not root.exists() or root.is_symlink():
        return skipped
    entries = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    entries.append(root)
    for path in entries:
        if path.is_symlink():
            continue
        try:
            current = path.stat().st_mode
            os.chmod(path, mode_fn(current))
        except FileNotFoundError:
            continue
        except OSError as exc:
            # A path this uid cannot chmod (EPERM on files owned by another
            # user, e.g. a root-baked container venv; EROFS on a read-only
            # mount). The read-only sweep records and skips it: usually such
            # files are unwritable to the runtime uid anyway, and killing the
            # session cannot protect them. The mutable (+w) sweep must stay
            # strict: opening a mutation window over a tree we cannot fully
            # unlock risks a half-applied destructive change.
            if strict:
                raise
            skipped.append(f"{path}: {exc}")
    return skipped


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
    except OSError:
        return False


def _distribution(name: str = "bobi"):
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def _dist_info_path(dist) -> Path | None:
    for file in dist.files or []:
        parts = Path(str(file)).parts
        if parts and parts[0].endswith(".dist-info"):
            located = Path(dist.locate_file(file))
            root = located
            for _ in range(max(len(parts) - 1, 0)):
                root = root.parent
            return root
    return None


def _is_editable_distribution(dist) -> bool:
    files = dist.files
    if not files:
        return True
    for file in files:
        if str(file).endswith("direct_url.json"):
            try:
                data = Path(dist.locate_file(file)).read_text()
            except OSError:
                continue
            if '"editable": true' in data or '"editable":true' in data:
                return True
    return False


def _looks_like_source_checkout(package_dir: Path) -> bool:
    for parent in [package_dir, *package_dir.parents]:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return True
    return False


def release_marker_path() -> Path:
    return paths.home_dir() / RELEASE_MARKER


def release_window_status(*, now: float | None = None) -> ReleaseWindowStatus:
    marker = release_marker_path()
    if not marker.exists():
        return ReleaseWindowStatus(marker, "missing", "no release marker")
    try:
        raw = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return ReleaseWindowStatus(marker, "invalid", f"unreadable marker: {exc}")
    if not isinstance(raw, dict):
        return ReleaseWindowStatus(marker, "invalid", "marker is not a JSON object")

    prefix_raw = raw.get("prefix")
    expires_at = raw.get("expires_at")
    opened_by = raw.get("opened_by")
    pid = raw.get("pid")
    marker_state = raw.get("state")
    if not isinstance(prefix_raw, str) or not prefix_raw:
        return ReleaseWindowStatus(marker, "invalid", "marker prefix is missing")
    prefix = Path(prefix_raw).expanduser()
    if not prefix.is_absolute():
        return ReleaseWindowStatus(marker, "invalid", "marker prefix is not absolute")
    try:
        prefix = prefix.resolve()
    except OSError as exc:
        return ReleaseWindowStatus(marker, "invalid", f"marker prefix is invalid: {exc}")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return ReleaseWindowStatus(marker, "invalid", "marker expiry is missing")
    expires_at = float(expires_at)
    if not math.isfinite(expires_at):
        return ReleaseWindowStatus(marker, "invalid", "marker expiry is not finite")
    if not isinstance(opened_by, str) or not opened_by.strip():
        return ReleaseWindowStatus(marker, "invalid", "marker opener is missing")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return ReleaseWindowStatus(marker, "invalid", "marker pid is invalid")
    if marker_state not in ("opening", "open"):
        return ReleaseWindowStatus(marker, "invalid", "marker state is invalid")

    common = dict(
        prefix=prefix,
        expires_at=expires_at,
        opened_by=opened_by,
        pid=pid,
    )
    if marker_state == "opening":
        return ReleaseWindowStatus(
            marker, "opening", "release window is still opening", **common)

    current = time.time() if now is None else now
    if expires_at <= current:
        return ReleaseWindowStatus(marker, "expired", "release window expired", **common)
    if expires_at > current + RELEASE_WINDOW + RELEASE_CLOCK_SKEW:
        return ReleaseWindowStatus(
            marker, "invalid", "marker expiry is too far in the future", **common)
    return ReleaseWindowStatus(marker, "open", "release window is open", **common)


def _framework_install_context() -> tuple[list[ProtectedRoot], str]:
    import bobi

    package = Path(bobi.__file__).resolve().parent
    dist = _distribution("bobi")
    if dist is None:
        return [], "Bobi distribution metadata was not found"
    if _is_editable_distribution(dist):
        return [], "editable install"
    if _looks_like_source_checkout(package):
        return [], "source checkout"

    roots = [ProtectedRoot(
        path=package,
        kind="bobi-package",
        reason="installed Bobi framework package",
    )]
    dist_info = _dist_info_path(dist)
    if dist_info and dist_info.exists():
        roots.append(ProtectedRoot(
            path=dist_info,
            kind="bobi-dist-info",
            reason="installed Bobi distribution metadata",
        ))
    return roots, ""


def framework_release_roots() -> tuple[list[ProtectedRoot], str]:
    """Installed framework roots and an honest reason when none exist."""
    return _framework_install_context()


def protected_runtime_roots(runtime_root: Path | None) -> list[ProtectedRoot]:
    roots: list[ProtectedRoot] = []
    if runtime_root is not None:
        package = paths.package_dir(runtime_root)
        if package.exists():
            roots.append(ProtectedRoot(
                path=package,
                kind="team-package",
                reason="installed agent package image",
            ))

    framework_roots, _ = _framework_install_context()
    bobi_package = framework_roots[0].path if framework_roots else None
    assigned_source = (
        bobi_package is not None
        and runtime_root is not None
        and _is_relative_to(bobi_package, runtime_root)
    )
    if not assigned_source:
        roots.extend(framework_roots)
    return roots


def _apply_runtime_write_policy(
    runtime_root: Path | None, *, honor_release_window: bool,
) -> GuardReport:
    report = GuardReport()
    initial_window = release_window_status() if honor_release_window else None
    for root in protected_runtime_roots(runtime_root):
        if (
            honor_release_window
            and root.kind in FRAMEWORK_KINDS
            and initial_window is not None
            and initial_window.covers(root.path)
        ):
            report.released.append(root)
            continue
        skipped = _chmod_tree(root.path, _readonly_mode)
        if skipped:
            logger.warning(
                "Runtime write guard could not chmod %d path(s) under %s "
                "(left as-is; first: %s)",
                len(skipped), root.path, skipped[0],
            )
            report.skipped.extend(skipped)
        if honor_release_window and root.kind in FRAMEWORK_KINDS:
            # Release can open after this apply read the marker but before its
            # read-only sweep finishes. Reconcile after the sweep so an old
            # apply cannot be the last writer and silently defeat the window.
            final_window = release_window_status()
            if final_window.covers(root.path):
                restore_failures = _chmod_tree(root.path, _mutable_mode)
                restore_failures.extend(_mutable_failures(root))
                if restore_failures:
                    logger.warning(
                        "Runtime write guard could not honor the newly opened "
                        "release window under %s (first: %s)",
                        root.path, restore_failures[0],
                    )
                    report.skipped.extend(restore_failures)
                    report.protected.append(root)
                    continue
                settled_window = release_window_status()
                if settled_window.covers(root.path):
                    report.released.append(root)
                    continue
                # Reapply can close the marker while this process is restoring
                # write bits. If so, converge back to the closed policy.
                relock_failures = _chmod_tree(root.path, _readonly_mode)
                report.skipped.extend(relock_failures)
        report.protected.append(root)
    return report


def apply_runtime_write_policy(runtime_root: Path | None) -> GuardReport:
    return _apply_runtime_write_policy(runtime_root, honor_release_window=True)


def _framework_prefix(roots: list[ProtectedRoot]) -> Path:
    interpreter_prefix = Path(sys.prefix).resolve()
    if all(_is_relative_to(root.path, interpreter_prefix) for root in roots):
        return interpreter_prefix
    return Path(os.path.commonpath([str(root.path.resolve()) for root in roots]))


def _write_release_marker(
    prefix: Path, opened_by: str, expires_at: float,
    *, state: Literal["opening", "open"],
) -> Path:
    marker = release_marker_path()
    # Legacy readers do not understand state. An expired opening marker keeps
    # those readers fail-closed while this process changes permissions.
    marker_expiry = 0 if state == "opening" else expires_at
    fsutil.atomic_write_json(
        marker,
        {
            "prefix": str(prefix),
            "expires_at": marker_expiry,
            "opened_by": opened_by,
            "pid": os.getpid(),
            "state": state,
        },
        indent=None,
        sort_keys=True,
    )
    return marker


def _mutable_failures(root: ProtectedRoot) -> list[str]:
    failures: list[str] = []
    if not root.path.exists():
        return failures
    for path in [root.path, *root.path.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{path}: could not verify writable mode ({exc})")
            continue
        if not mode & stat.S_IWUSR:
            failures.append(f"{path}: owner write bit is not set")
    return failures


def root_mode_state(root: ProtectedRoot) -> Literal["locked", "released", "partial"]:
    writable = 0
    locked = 0
    if not root.path.exists():
        return "locked"
    for path in [root.path, *root.path.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
        except OSError:
            locked += 1
            continue
        if mode & stat.S_IWUSR:
            writable += 1
        else:
            locked += 1
    if writable and locked:
        return "partial"
    return "released" if writable else "locked"


def _close_release_marker() -> str:
    marker = release_marker_path()
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        return f"{marker}: could not close release window ({exc})"
    return ""


def release_runtime_write_policy(
    *, opened_by: str = "bobi guard release", now: float | None = None,
) -> ReleaseReport:
    """Open a bounded window and unlock package-manager-owned framework roots."""
    roots, reason = framework_release_roots()
    report = ReleaseReport(roots=roots, opened_by=opened_by, noop_reason=reason)
    if not roots:
        return report

    current = time.time() if now is None else now
    prefix = _framework_prefix(roots)
    report.prefix = prefix
    report.expires_at = current + RELEASE_WINDOW
    try:
        report.marker_path = _write_release_marker(
            prefix, opened_by, report.expires_at, state="opening")
    except OSError as exc:
        report.marker_path = release_marker_path()
        report.skipped.append(f"{report.marker_path}: could not open release window ({exc})")
        return report

    unlock_errors: list[str] = []
    for root in roots:
        unlock_errors.extend(_chmod_tree(root.path, _mutable_mode))

    # An apply that read the old closed state can finish after the first +w
    # sweep. Re-check and retry once so that race becomes visible or repaired.
    failed_roots = [root for root in roots if _mutable_failures(root)]
    for root in failed_roots:
        unlock_errors.extend(_chmod_tree(root.path, _mutable_mode))

    opening_failures = [
        failure
        for root in roots
        for failure in _mutable_failures(root)
    ]
    opening_failures = [*unlock_errors, *opening_failures]
    if opening_failures:
        report.skipped.extend(opening_failures)
        marker_error = _close_release_marker()
        if marker_error:
            report.skipped.append(marker_error)
        for root in roots:
            report.skipped.extend(_chmod_tree(root.path, _readonly_mode))
        return report

    try:
        report.marker_path = _write_release_marker(
            prefix, opened_by, report.expires_at, state="open")
    except OSError as exc:
        report.skipped.append(
            f"{report.marker_path}: could not activate release window ({exc})")
        marker_error = _close_release_marker()
        if marker_error:
            report.skipped.append(marker_error)
        for root in roots:
            report.skipped.extend(_chmod_tree(root.path, _readonly_mode))
        return report

    # An apply that saw the transient opening state can finish after the first
    # verification. Sweep once more after activation; any still-later apply
    # performs its own post-sweep reconciliation against the open marker.
    final_failures: list[str] = []
    for root in roots:
        final_failures.extend(_chmod_tree(root.path, _mutable_mode))
        final_failures.extend(_mutable_failures(root))

    window = release_window_status(now=current)
    if not window.is_open or any(not window.covers(root.path) for root in roots):
        final_failures.append(
            f"{report.marker_path}: release window did not remain open ({window.detail})")
    if final_failures:
        report.skipped.extend(final_failures)
        marker_error = _close_release_marker()
        if marker_error:
            report.skipped.append(marker_error)
        for root in roots:
            report.skipped.extend(_chmod_tree(root.path, _readonly_mode))
        return report

    report.released.extend(roots)
    return report


def reapply_runtime_write_policy(runtime_root: Path | None = None) -> ReapplyReport:
    marker = release_marker_path()
    marker_error = _close_release_marker()
    guard = _apply_runtime_write_policy(runtime_root, honor_release_window=False)
    return ReapplyReport(guard=guard, marker_path=marker, marker_error=marker_error)


def root_write_failures(
    root: ProtectedRoot, *, include_writable: bool = True,
    include_symlink_failures: bool = True,
) -> list[str]:
    failures: list[str] = []
    if not root.path.exists():
        return failures
    for path in [root.path, *root.path.rglob("*")]:
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink():
            if not include_symlink_failures:
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                failures.append(f"{path}: unreadable symlink target ({exc})")
                continue
            if not _is_relative_to(resolved, root.path):
                failures.append(f"{path}: symlink escapes protected root")
            continue
        if include_writable and _writable_bits(st.st_mode):
            failures.append(f"{path}: writable mode {stat.filemode(st.st_mode)}")
    return failures


def check_runtime_write_policy(runtime_root: Path | None) -> PolicyCheck:
    roots = protected_runtime_roots(runtime_root)
    window = release_window_status()
    released: list[ProtectedRoot] = []
    failures: list[str] = []
    for root in roots:
        covered = root.kind in FRAMEWORK_KINDS and window.covers(root.path)
        if covered:
            released.append(root)
        failures.extend(root_write_failures(root, include_writable=not covered))
    if failures:
        shown = "; ".join(failures[:3])
        suffix = "..." if len(failures) > 3 else ""
        return PolicyCheck(
            ok=False,
            detail=f"{len(failures)} writable/unsafe runtime file(s): {shown}{suffix}",
            protected=roots,
            released=released,
            failures=failures,
            window=window,
        )
    detail = f"{len(roots)} protected runtime root(s)"
    if released:
        detail += (
            f"; release window open for {len(released)} framework root(s) "
            f"by {window.opened_by} (pid {window.pid}) until {window.expires_at}"
        )
    elif window.state == "invalid":
        detail += f"; invalid release marker ignored: {window.detail}"
    return PolicyCheck(
        ok=True,
        detail=detail,
        protected=roots,
        released=released,
        window=window,
    )


@contextlib.contextmanager
def with_mutable_runtime_package(runtime_root: Path) -> Iterator[None]:
    package = paths.package_dir(runtime_root)
    # The unlock is INSIDE the try: the strict sweep raises partway through on
    # a file this uid cannot chmod (EPERM on another user's file, EROFS on a
    # read-only mount), and running it before the try meant every file already
    # opened stayed writable with no rollback (D044) — a half-unlocked
    # protected tree that fails doctor's write-policy check until some later
    # spawn re-runs prepare_brain_runtime. The error still propagates; the
    # tree is re-locked first.
    try:
        if package.exists():
            _chmod_tree(package, _mutable_mode, strict=True)
        yield
    finally:
        if package.exists():
            _chmod_tree(package, _readonly_mode)


def prepare_brain_runtime(runtime_root: Path | None = None) -> GuardReport:
    if runtime_root is None:
        try:
            runtime_root = paths.bound_root()
        except Exception:
            runtime_root = None
    return apply_runtime_write_policy(runtime_root)


def _record_digest(file) -> tuple[str, str] | None:
    hash_obj = getattr(file, "hash", None)
    if hash_obj is None or getattr(hash_obj, "mode", "") != "sha256":
        return None
    value = getattr(hash_obj, "value", "")
    if not value:
        return None
    return "sha256", value


def _console_script_names(dist) -> set[str]:
    names: set[str] = set()
    for entry in getattr(dist, "entry_points", ()) or ():
        if getattr(entry, "group", "") != "console_scripts":
            continue
        name = str(getattr(entry, "name", "") or "")
        if name:
            names.add(name)
            names.add(f"{name}.exe")
            names.add(f"{name}-script.py")
    return names


def _urlsafe_b64_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def check_bobi_distribution_integrity(dist=None) -> PolicyCheck:
    dist = dist if dist is not None else _distribution("bobi")
    if dist is None:
        return PolicyCheck(ok=True, detail="bobi distribution metadata not found")
    if _is_editable_distribution(dist):
        return PolicyCheck(ok=True, detail="editable/source install")
    if not dist.files:
        return PolicyCheck(ok=True, detail="no RECORD metadata")

    import bobi

    package_root = Path(bobi.__file__).resolve().parent
    dist_info = _dist_info_path(dist)
    allowed_roots = [package_root]
    if dist_info is not None:
        allowed_roots.append(dist_info)
    console_scripts = _console_script_names(dist)

    failures: list[str] = []
    checked = 0
    for file in dist.files:
        digest = _record_digest(file)
        if digest is None:
            continue
        located = Path(dist.locate_file(file)).resolve()
        if not any(_is_relative_to(located, root) for root in allowed_roots):
            if located.name in console_scripts:
                continue
            failures.append(f"{file}: resolves outside Bobi distribution roots")
            continue
        try:
            data = located.read_bytes()
        except FileNotFoundError:
            failures.append(f"{file}: missing")
            continue
        except OSError as exc:
            failures.append(f"{file}: unreadable ({exc})")
            continue
        checked += 1
        if _urlsafe_b64_sha256(data) != digest[1]:
            failures.append(f"{file}: sha256 mismatch")

    if failures:
        shown = "; ".join(failures[:3])
        suffix = "..." if len(failures) > 3 else ""
        return PolicyCheck(
            ok=False,
            detail=f"{len(failures)} Bobi install integrity issue(s): {shown}{suffix}",
            failures=failures,
        )
    return PolicyCheck(ok=True, detail=f"{checked} hashed Bobi file(s) verified")
