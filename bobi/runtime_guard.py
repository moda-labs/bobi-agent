"""Runtime filesystem write policy for Bobi-owned install roots."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.metadata as metadata
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

from bobi import paths

logger = logging.getLogger(__name__)

ProtectedKind = Literal[
    "team-package",
    "bobi-package",
    "bobi-dist-info",
    "venv",
    "dependency",
]


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
    if dist is None:
        return False
    try:
        direct_url = dist.read_text("direct_url.json")
        if direct_url and ('"editable": true' in direct_url or '"editable":true' in direct_url):
            return True
    except Exception:
        pass
    files = getattr(dist, "files", None)
    if files:
        for file in files:
            if str(file).endswith("direct_url.json"):
                try:
                    data = Path(dist.locate_file(file)).read_text()
                    if '"editable": true' in data or '"editable":true' in data:
                        return True
                except OSError:
                    continue
    return False


def _is_direct_source_checkout(package_dir: Path) -> bool:
    repo_root = package_dir.parent
    return (repo_root / "pyproject.toml").is_file() and (repo_root / ".git").exists()


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
    return roots


def apply_runtime_write_policy(runtime_root: Path | None) -> GuardReport:
    report = GuardReport()
    for root in protected_runtime_roots(runtime_root):
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


def _check_root(root: ProtectedRoot) -> list[str]:
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
    failures: list[str] = []
    for root in roots:
        failures.extend(_check_root(root))
    if failures:
        shown = "; ".join(failures[:3])
        suffix = "..." if len(failures) > 3 else ""
        return PolicyCheck(
            ok=False,
            detail=f"{len(failures)} writable/unsafe runtime file(s): {shown}{suffix}",
            protected=roots,
            failures=failures,
        )
    return PolicyCheck(
        ok=True,
        detail=f"{len(roots)} protected runtime root(s)",
        protected=roots,
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


_UNSET = object()


def verify_framework_integrity_or_raise(dist: Any = _UNSET) -> PolicyCheck:
    """Verify Bobi framework files against PEP 376 RECORD digests (FIM).

    Fails closed by raising RuntimeError if any framework file is missing,
    unreadable, or has a mismatched SHA-256 digest.
    """
    # The launch gate is the ONE caller that tolerates a changed digest on the
    # event-server build inputs; see the exemption note above.
    check = check_bobi_distribution_integrity(
        dist=dist,
        tolerate_event_server_build_inputs=True,
    )
    if not check.ok:
        raise RuntimeError(
            f"Bobi framework integrity violation detected: {check.detail}. "
            "Installed framework files appear modified or corrupted. "
            "Reinstall the exact version (`pip install --force-reinstall "
            "--no-deps bobi==<version>`); installed package files are immutable "
            "and cannot be repaired in place. If a container image rebuilds or "
            "overlays anything under site-packages/bobi, drop that step: the "
            "wheel already ships the runnable event-server bundle."
        )
    return check


def prepare_brain_runtime(runtime_root: Path | None = None) -> GuardReport:
    """Prepare the runtime environment before an agent session or workflow step.

    1. Enforces File Integrity Monitoring (FIM) over the Bobi framework distribution.
    2. Applies read-only permissions to the bound team package image.
    """
    if runtime_root is None:
        try:
            runtime_root = paths.bound_root()
        except Exception:
            runtime_root = None
    verify_framework_integrity_or_raise()
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


# Event-server paths whose digest the fail-closed startup gate tolerates.
#
# An installed Bobi loads exactly one file out of bobi/event-server/: the
# prebuilt dist/local.js bundle. events.server._validate_packaged_artifact
# re-validates it on every start against dist/local.inputs.json (digest and
# size of the bundle and the notice, the bundled-dependency inventory, the
# build-tool versions), so dist/ keeps both this gate and its own audit.
#
# The waiver is exactly the two npm-owned manifests, not every build input.
# npm is the only thing that legitimately rewrites a shipped file here, and it
# rewrites only these two; the TypeScript sources and tsconfigs stay fully
# gated, because nothing legitimate edits them in an installed tree and a
# change there is a real "your install is modified" signal.
#
# Neither manifest is read AS CODE by an installed Bobi.
# artifact.locked_esbuild_version, the only lockfile reader, is reached only
# under validate_artifact(verify_inputs=True), which the source path alone
# uses.
#
# "Not as code" is the exact claim, not "not at all": Node resolves the
# nearest package.json for its `type` field when it loads dist/local.js, so
# flipping that field to "module" breaks the CommonJS bundle with `require is
# not defined in ES module scope`. That is a loud load failure, not attacker
# code execution, so tolerating its digest is still right - but do not widen
# this exemption on the belief that nothing reads these files at all.
#
# Only the LAUNCH gate relaxes, and only on the digest: full verification is
# the default, and verify_framework_integrity_or_raise opts into the
# tolerance. Every other caller (doctor) keeps reporting a modified input. A
# missing or unreadable input still fails closed here too, because
# _find_event_server_dir probes for package.json, so an absent one breaks
# startup for real.
#
# The launch gate relaxes because the shipped tree is an npm workspace root
# whose `worker` workspace is deliberately not distributed. Any npm command
# that re-resolves the graph (`npm install`, `npm dedupe`, `npm audit fix`)
# therefore prunes that workspace's transitive entries and rewrites
# package-lock.json in place, which crash-looped every restart on a file the
# runtime does not read (#1087). `npm ci` does not do this, so the documented
# build is unaffected.
_EVENT_SERVER_NPM_MANIFESTS = frozenset(
    {
        "bobi/event-server/package.json",
        "bobi/event-server/package-lock.json",
    }
)


def _is_event_server_build_input(record_path: str) -> bool:
    return record_path in _EVENT_SERVER_NPM_MANIFESTS


def check_bobi_distribution_integrity(
    dist: Any = _UNSET,
    *,
    tolerate_event_server_build_inputs: bool = False,
) -> PolicyCheck:
    import bobi

    package_root = Path(bobi.__file__).resolve().parent

    resolved_dist = _distribution("bobi") if dist is _UNSET else dist
    if resolved_dist is None:
        if _is_direct_source_checkout(package_root):
            return PolicyCheck(ok=True, detail="source checkout")
        return PolicyCheck(ok=False, detail="bobi distribution metadata not found")

    if _is_editable_distribution(resolved_dist) or _is_direct_source_checkout(package_root):
        return PolicyCheck(ok=True, detail="editable/source install")
    if not getattr(resolved_dist, "files", None):
        return PolicyCheck(ok=False, detail="no RECORD metadata")

    dist_info = _dist_info_path(resolved_dist)
    allowed_roots = [package_root]
    if dist_info is not None:
        allowed_roots.append(dist_info)
    console_scripts = _console_script_names(resolved_dist)

    failures: list[str] = []
    waived: list[str] = []
    checked = 0
    for file in resolved_dist.files:
        digest = _record_digest(file)
        if digest is None:
            continue
        located = Path(resolved_dist.locate_file(file)).resolve()
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
        if _urlsafe_b64_sha256(data) == digest[1]:
            checked += 1
            continue
        if tolerate_event_server_build_inputs and _is_event_server_build_input(
            Path(str(file)).as_posix()
        ):
            # Counted as waived, never as verified: reporting "N verified" over
            # a file that just failed its digest would launder the mismatch.
            waived.append(str(file))
            continue
        failures.append(f"{file}: sha256 mismatch")

    if waived:
        # prepare_brain_runtime discards the PolicyCheck, so without this the
        # waiver leaves no trace anywhere in a live deployment.
        logger.warning(
            "Launch gate tolerated %d modified event-server manifest(s) the "
            "installed runtime does not execute: %s. Reinstall to restore the "
            "shipped bytes; `bobi agent <name> doctor` reports them in full.",
            len(waived),
            ", ".join(sorted(waived)),
        )

    if failures:
        shown = "; ".join(failures[:3])
        suffix = "..." if len(failures) > 3 else ""
        return PolicyCheck(
            ok=False,
            detail=f"{len(failures)} Bobi install integrity issue(s): {shown}{suffix}",
            failures=failures,
        )
    if checked == 0:
        return PolicyCheck(
            ok=False,
            detail="0 hashed Bobi file(s) verified (missing SHA-256 RECORD entries)",
        )
    detail = f"{checked} hashed Bobi file(s) verified"
    if waived:
        detail += f"; {len(waived)} event-server manifest(s) tolerated"
    return PolicyCheck(ok=True, detail=detail)
