"""Guard the hatch build config so `python -m build` can't break again.

`python -m build` builds the wheel FROM the sdist. So every path the wheel
force-includes must also live in the sdist, or the wheel build dies with
"Forced include not found" (this happened for 0.22.0 when bundled-template
dirs under agents/ were force-included into the wheel but agents/ was missing
from the sdist include list).
"""
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _config():
    return tomllib.loads(PYPROJECT.read_text())


def test_every_wheel_force_include_is_in_sdist():
    cfg = _config()
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist_include = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    force_include = wheel.get("force-include", {})
    # Top-level path segment each sdist include entry covers.
    sdist_roots = {entry.split("/", 1)[0] for entry in sdist_include}

    missing = []
    for src in force_include:
        root = src.split("/", 1)[0]
        if root not in sdist_roots:
            missing.append(src)

    assert not missing, (
        f"wheel force-include source(s) not covered by sdist include "
        f"(build-from-sdist will fail): {missing}; sdist roots={sorted(sdist_roots)}"
    )


def test_force_included_template_paths_exist_on_disk():
    """The bundled-template source dirs must actually exist in the repo."""
    cfg = _config()
    force_include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    repo = PYPROJECT.parent
    missing = [src for src in force_include if not (repo / src).exists()]
    assert not missing, f"force-include sources missing on disk: {missing}"


# --- deploy assets (binary-mode `modastack deploy`) -------------------------

def test_deploy_assets_force_included_under_deploy_dir():
    """`modastack deploy` resolves its mechanics from modastack/_deploy in an
    installed wheel (binary mode). Guard that the Dockerfile + docker/ + scripts/
    are force-included there — a broken mapping silently disables binary deploy."""
    cfg = _config()
    force_include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    wanted = {
        "Dockerfile": "modastack/_deploy/Dockerfile",
        "docker/docker-entrypoint.sh": "modastack/_deploy/docker/docker-entrypoint.sh",
        "scripts/provision-instance.sh": "modastack/_deploy/scripts/provision-instance.sh",
        "scripts/destroy-instance.sh": "modastack/_deploy/scripts/destroy-instance.sh",
    }
    for src, dest in wanted.items():
        assert force_include.get(src) == dest, (
            f"deploy asset '{src}' must force-include to '{dest}' (got "
            f"{force_include.get(src)!r}) — binary `modastack deploy` needs it."
        )


# (A real `python -m build` + wheel-inspection guard lived here, but `build`
# isn't in the unit-test venv and the actual wheel is built for real by
# container.yml + publish-pypi; the config-level guards above — force-include
# mapping + in-sdist + source-exists — already catch every way the deploy
# assets can drop out of the wheel.)


# --- Dockerfile build modes (binary deploy + lean image) --------------------

def test_dockerfile_has_source_and_pypi_build_modes():
    """One Dockerfile, MODASTACK_BUILD={source|pypi}. Guard the stages + the
    arg-selected builder so binary mode can't silently regress to source-only."""
    df = (PYPROJECT.parent / "Dockerfile").read_text()
    assert "FROM builder-base AS builder-source" in df
    assert "FROM builder-base AS builder-pypi" in df
    assert "FROM builder-${MODASTACK_BUILD} AS builder" in df
    assert "ARG MODASTACK_BUILD" in df


def test_dockerfile_pypi_stage_installs_fastembed_not_kb_extra():
    """The pypi builder must install fastembed EXPLICITLY, never `modastack[kb]`
    — some published `[kb]` extras stale-list sentence-transformers → torch +
    ~2 GB CUDA the dark CPU instance never uses (and that blows the build)."""
    df = (PYPROJECT.parent / "Dockerfile").read_text()
    pypi = df.split("AS builder-pypi", 1)[1].split("AS builder", 1)[0]
    # The actual install invocation — skip comment lines, which legitimately
    # mention [kb]/sentence-transformers to say "don't use them".
    install = " ".join(l for l in pypi.splitlines() if not l.lstrip().startswith("#"))
    assert "fastembed" in install and "sqlite-vec" in install
    assert "modastack[kb]" not in install
