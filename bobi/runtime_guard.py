"""Runtime filesystem write policy for Bobi-owned install roots."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.metadata as metadata
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

from bobi import paths

logger = logging.getLogger(__name__)

ProtectedKind = Literal[
    "team-package",
    "bobi-package",
    "bobi-dist-info",
    "venv",
    "dependency",
]

# The protected roots that sit inside a package manager's prefix. Bobi locks
# them, but uv/pipx/brew own their lifecycle: replacing Bobi means deleting
# these trees, which a read-only directory forbids.
FRAMEWORK_KINDS: tuple[ProtectedKind, ...] = ("bobi-package", "bobi-dist-info")

# How long `bobi guard release` holds the framework roots unlocked. Long enough
# for a package manager to resolve, build, and swap the tree (a git dependency
# takes minutes, not seconds); short enough that a forgotten release re-locks
# itself with no operator action.
RELEASE_WINDOW = 15 * 60


@dataclass(frozen=True)
class ProtectedRoot:
    path: Path
    kind: ProtectedKind
    mode: Literal["readonly"] = "readonly"
    reason: str = ""


@dataclass
class GuardReport:
    protected: list[ProtectedRoot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    released: list[ProtectedRoot] = field(default_factory=list)


@dataclass
class ReleaseReport:
    released: list[ProtectedRoot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    expires_at: float = 0.0
    install_path: Path | None = None
    # Why there was nothing to release, when `released` is empty.
    reason: str = ""
    # Set when the release window could not be opened, so a concurrent agent
    # launch may re-lock the tree before the upgrade finishes.
    window_error: str = ""


@dataclass
class PolicyCheck:
    ok: bool
    detail: str = ""
    protected: list[ProtectedRoot] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


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

    import bobi

    bobi_package = Path(bobi.__file__).resolve().parent
    dist = _distribution("bobi")
    editable = _is_editable_distribution(dist) if dist is not None else True
    source_checkout = _looks_like_source_checkout(bobi_package)
    assigned_source = (
        runtime_root is not None
        and _is_relative_to(bobi_package, runtime_root)
    )
    if not editable and not source_checkout and not assigned_source:
        roots.append(ProtectedRoot(
            path=bobi_package,
            kind="bobi-package",
            reason="installed Bobi framework package",
        ))
        dist_info = _dist_info_path(dist)
        if dist_info and dist_info.exists():
            roots.append(ProtectedRoot(
                path=dist_info,
                kind="bobi-dist-info",
                reason="installed Bobi distribution metadata",
            ))
    return roots


def release_marker_path() -> Path:
    """Marker file whose mtime opens the release window.

    Deliberately outside every protected root and outside any package-manager
    prefix. It has to survive the framework tree being locked, survive that
    tree being half-deleted by a failed upgrade, and stay writable by
    `scripts/install.sh` with a plain `touch` at the point where the `bobi`
    entrypoint no longer imports.
    """
    return paths.home_dir() / "runtime-guard-released"


def release_window_expires_at() -> float | None:
    """Epoch seconds at which an open release window closes, else None."""
    try:
        mtime = release_marker_path().stat().st_mtime
    except OSError:
        return None
    expires = mtime + RELEASE_WINDOW
    return expires if expires > time.time() else None


def open_release_window() -> float:
    marker = release_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()  # on an existing marker this is what restarts the window
    return marker.stat().st_mtime + RELEASE_WINDOW


def close_release_window() -> bool:
    try:
        release_marker_path().unlink()
        return True
    except OSError:
        return False


def apply_runtime_write_policy(runtime_root: Path | None) -> GuardReport:
    report = GuardReport()
    released = release_window_expires_at() is not None
    for root in protected_runtime_roots(runtime_root):
        if released and root.kind in FRAMEWORK_KINDS:
            # An operator opened a release window to upgrade Bobi. Re-locking
            # now would break that upgrade mid-flight: agent launches land far
            # more often than a package manager takes to swap the tree, so the
            # documented steps would fail for reasons the operator cannot see
            # (#858). The window is bounded, so this heals itself.
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
        report.protected.append(root)
    return report


def _unreleasable_reason() -> str:
    """Why `protected_runtime_roots` yielded no framework roots."""
    import bobi

    dist = _distribution("bobi")
    if dist is None:
        return "no installed Bobi distribution metadata"
    if _is_editable_distribution(dist):
        return "editable install"
    if _looks_like_source_checkout(Path(bobi.__file__).resolve().parent):
        return "source checkout"
    return "not a package-managed install"


def release_runtime_write_policy() -> ReleaseReport:
    """Unlock Bobi's own installed package so a package manager can replace it.

    Restores write bits on the framework roots only. The team-package image is
    not package-manager-owned and has its own mutation window in
    `with_mutable_runtime_package`, so releasing it here would open a hole no
    upgrade needs.
    """
    import bobi

    report = ReleaseReport()
    report.install_path = Path(bobi.__file__).resolve().parent
    # Open the window BEFORE unlocking. An agent launch between the first chmod
    # and the last would otherwise re-lock exactly what was just released.
    try:
        report.expires_at = open_release_window()
    except OSError as exc:
        # Unlocking is still worth doing, but without the window a running team
        # can re-lock the tree mid-upgrade. Say so rather than reporting a
        # protection the operator does not have.
        report.window_error = str(exc)

    roots = [r for r in protected_runtime_roots(None) if r.kind in FRAMEWORK_KINDS]
    if not roots:
        report.reason = _unreleasable_reason()
        return report

    for root in roots:
        # Best-effort, unlike the strict mutation-window sweep: there is no
        # re-lock to keep consistent here, and stopping at the first EPERM
        # would leave a half-unlocked tree with no report - the silent state
        # that makes this failure unguessable in the first place.
        report.skipped.extend(_chmod_tree(root.path, _mutable_mode))
        report.released.append(root)
    return report


def reapply_runtime_write_policy(runtime_root: Path | None = None) -> GuardReport:
    """Close any release window and re-lock immediately."""
    close_release_window()
    return apply_runtime_write_policy(runtime_root)


def root_write_failures(root: ProtectedRoot) -> list[str]:
    """Paths under `root` that are writable or unsafe (empty when locked)."""
    failures: list[str] = []
    if not root.path.exists():
        return failures
    for path in [root.path, *root.path.rglob("*")]:
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                failures.append(f"{path}: unreadable symlink target ({exc})")
                continue
            if not _is_relative_to(resolved, root.path):
                failures.append(f"{path}: symlink escapes protected root")
            continue
        if _writable_bits(st.st_mode):
            failures.append(f"{path}: writable mode {stat.filemode(st.st_mode)}")
    return failures


def check_runtime_write_policy(runtime_root: Path | None) -> PolicyCheck:
    roots = protected_runtime_roots(runtime_root)
    expires = release_window_expires_at()
    failures: list[str] = []
    for root in roots:
        # Framework roots are writable on purpose during a release window.
        # Reporting them as drift would train operators to ignore this check
        # every time they upgrade.
        if expires and root.kind in FRAMEWORK_KINDS:
            continue
        failures.extend(root_write_failures(root))
    if failures:
        shown = "; ".join(failures[:3])
        suffix = "..." if len(failures) > 3 else ""
        return PolicyCheck(
            ok=False,
            detail=f"{len(failures)} writable/unsafe runtime file(s): {shown}{suffix}",
            protected=roots,
            failures=failures,
        )
    detail = f"{len(roots)} protected runtime root(s)"
    if expires:
        until = time.strftime("%H:%M", time.localtime(expires))
        detail += (f"; framework roots released until {until} "
                   "(`bobi guard reapply` re-locks now)")
    elif any(root.kind in FRAMEWORK_KINDS for root in roots):
        # Bobi's own install is locked, so the next `uv tool install --force` /
        # `pipx upgrade` / `brew upgrade` cannot delete the tree it must
        # replace. Name the step here, where an operator still has a CLI.
        detail += "; run `bobi guard release` before upgrading Bobi"
    return PolicyCheck(ok=True, detail=detail, protected=roots)


@contextlib.contextmanager
def with_mutable_runtime_package(runtime_root: Path) -> Iterator[None]:
    package = paths.package_dir(runtime_root)
    if package.exists():
        _chmod_tree(package, _mutable_mode, strict=True)
    try:
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
