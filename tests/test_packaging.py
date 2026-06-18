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
