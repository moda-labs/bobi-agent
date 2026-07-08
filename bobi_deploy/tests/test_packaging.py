"""Guard the bobi-deploy packaging + the instance Dockerfile's build modes.

The deploy assets (Dockerfile, docker/, provision/destroy/fleet scripts) ship
in THIS package's wheel under bobi_deploy/_deploy (#707) - a broken mapping
silently disables binary-mode `bobi build`/`bobi deploy`. The sources are
../-relative (repo root) while the package lives inside the bobi repo, so the
wheel must be built from the repo tree; there is no build-from-sdist invariant
to guard here (unlike the public package's own packaging tests).
"""
import re
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

PKG_ROOT = Path(__file__).resolve().parent.parent  # bobi_deploy/
REPO_ROOT = PKG_ROOT.parent
PYPROJECT = PKG_ROOT / "pyproject.toml"


def _force_include():
    cfg = tomllib.loads(PYPROJECT.read_text())
    return cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def test_deploy_assets_force_included_under_deploy_dir():
    """Binary mode resolves its mechanics from bobi_deploy/_deploy in an
    installed wheel (build.py:_packaged_deploy_dir). Guard the mapping
    mechanically over EVERY entry - a hand-typed subset would let a new or
    dropped asset drift past - and pin the assets the engine reads by name."""
    force_include = _force_include()
    for src, dest in force_include.items():
        assert dest == "bobi_deploy/_deploy/" + src.removeprefix("../"), (
            f"deploy asset '{src}' maps to '{dest}' - binary mode reads "
            f"bobi_deploy/_deploy/<repo-relative path>, so the mapping must "
            f"be mechanical."
        )
    # The engine's hardcoded reads (resolve_assets): these exact files must
    # stay in the bundle, whatever else the mapping carries.
    dests = set(force_include.values())
    for needed in (
        "bobi_deploy/_deploy/Dockerfile",
        "bobi_deploy/_deploy/docker/docker-entrypoint.sh",
        "bobi_deploy/_deploy/docker/noop-deps.sh",
        "bobi_deploy/_deploy/docker/healthcheck.sh",
        "bobi_deploy/_deploy/scripts/provision-instance.sh",
        "bobi_deploy/_deploy/scripts/destroy-instance.sh",
        "bobi_deploy/_deploy/scripts/fleet.sh",
    ):
        assert needed in dests, (
            f"'{needed}' missing from the bundled deploy assets - binary "
            f"`bobi build`/`bobi deploy` need it."
        )


def test_force_include_sources_exist_on_disk():
    """Every bundled asset source must actually exist, or the wheel build
    dies with "Forced include not found"."""
    missing = [src for src in _force_include()
               if not (PKG_ROOT / src).resolve().exists()]
    assert not missing, f"force-include sources missing on disk: {missing}"


# --- Dockerfile build modes (binary deploy + lean image) --------------------
# The instance Dockerfile is deploy IP; its guards live with the deploy
# package (#707) so they move private with the file at cut time.

def test_dockerfile_has_source_and_pypi_build_modes():
    """One Dockerfile, BOBI_BUILD={source|pypi}. Guard the stages + the
    arg-selected builder so binary mode can't silently regress to source-only."""
    df = (REPO_ROOT / "Dockerfile").read_text()
    assert "FROM builder-base AS builder-source" in df
    assert "FROM builder-base AS builder-pypi" in df
    assert "FROM builder-${BOBI_BUILD} AS builder" in df
    assert "ARG BOBI_BUILD" in df


def test_dockerfile_pypi_stage_installs_fastembed_not_kb_extra():
    """The pypi builder must install fastembed EXPLICITLY, never `bobi[kb]`
    — some published `[kb]` extras stale-list sentence-transformers → torch +
    ~2 GB CUDA the dark CPU instance never uses (and that blows the build)."""
    df = (REPO_ROOT / "Dockerfile").read_text()
    pypi = df.split("AS builder-pypi", 1)[1].split("AS builder", 1)[0]
    # The actual install invocation — skip comment lines, which legitimately
    # mention [kb]/sentence-transformers to say "don't use them".
    install = " ".join(l for l in pypi.splitlines() if not l.lstrip().startswith("#"))
    assert "fastembed" in install and "sqlite-vec" in install
    assert "bobi[kb]" not in install


def test_dockerfile_pins_aichat_version():
    """aichat (the baked LLM gateway CLI) must be pinned to an exact version so
    two builds of the same commit produce identical layers (cf. #380). A floating
    download would let an upstream bump land with no diff to point at."""
    df = (REPO_ROOT / "Dockerfile").read_text()
    assert re.search(r"ARG AICHAT_VERSION=\d+\.\d+\.\d+", df), \
        "AICHAT_VERSION must be pinned to an exact x.y.z"
    # The install URL must reference the pinned arg, not a floating tag.
    assert "aichat-v${AICHAT_VERSION}-" in df and "releases/download/v${AICHAT_VERSION}/" in df
