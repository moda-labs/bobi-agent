"""The shared web-UI tokens must stay in sync with the Bobi design system.

`bobi/webui_common/static/tokens.css` is a hand-maintained, offline, vanilla-CSS
port of `docs/design-system/tokens/`. Nothing regenerates it, so without
a guard the two drift silently and the local UIs slowly stop looking like the
product. These tests pin the port to the source.
"""

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent
TOKENS_CSS = ROOT / "bobi" / "webui_common" / "static" / "tokens.css"
FONTS_CSS = ROOT / "bobi" / "webui_common" / "static" / "fonts.css"
FONTS_DIR = ROOT / "bobi" / "webui_common" / "static" / "fonts"
DESIGN_SYSTEM = ROOT / "docs" / "design-system" / "tokens"
CHROME_CSS = ROOT / "bobi" / "webui_common" / "static" / "chrome.css"
APP_CSS = [
    ROOT / "bobi" / "setup" / "webui" / "static" / "app.css",
    ROOT / "bobi" / "webapp" / "static" / "app.css",
    CHROME_CSS,
]

# Bobi is a sub-brand of Moda Labs and changes exactly one thing: the accent.
# Moda's signature red must never appear on a Bobi surface.
MODA_RED = "#FD4235"

# The pre-2026-07-31 amber accent, retired by the design-system reskin.
RETIRED_AMBER = ("#C8612B", "#D86E33", "#E0843F")


def _declarations(css: str) -> dict[str, str]:
    """Custom-property declarations, last one winning (as the cascade does)."""
    return dict(re.findall(r"(?m)^\s*(--[\w-]+)\s*:\s*([^;]+);", css))


def _without_comments(css: str) -> str:
    """Drop /* ... */ so colour bans test what is PAINTED, not what is documented."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_shared_tokens_match_design_system_values():
    """Every --bobi-* brand token matches the design system's own value."""
    source = _declarations((DESIGN_SYSTEM / "colors.css").read_text())
    ours = _declarations(TOKENS_CSS.read_text())

    brand = {k: v for k, v in source.items() if k.startswith("--bobi-")}
    assert brand, "design system colors.css declared no --bobi-* tokens"

    for token, value in brand.items():
        assert token in ours, f"{token} missing from the port"
        assert ours[token] == value, (
            f"{token} drifted: design system has {value!r}, "
            f"port has {ours[token]!r}"
        )


def test_accent_is_violet_and_moda_red_never_appears():
    css = TOKENS_CSS.read_text()
    ours = _declarations(css)
    # Dusk violet, declared sRGB-first then oklch so browsers without oklch()
    # still get a working accent.
    assert ours["--bobi-acc"].startswith("oklch(")
    assert "#6556c8" in css, "sRGB fallback for the accent went missing"
    assert ours["--bobi-clay"] == "#D67B55"
    for path in [TOKENS_CSS, *APP_CSS]:
        painted = _without_comments(path.read_text()).lower()
        assert MODA_RED.lower() not in painted, path


def test_retired_amber_accent_is_gone():
    for path in [TOKENS_CSS, *APP_CSS]:
        painted = _without_comments(path.read_text()).upper()
        for retired in RETIRED_AMBER:
            assert retired not in painted, f"{retired} still in {path}"


def test_app_styles_do_not_redeclare_design_tokens():
    """App sheets consume tokens; only the shared sheet defines them."""
    design_token_names = {
        k for k in _declarations(TOKENS_CSS.read_text())
        if k.startswith(("--bobi-", "--surface-", "--text-", "--border-",
                         "--status-", "--font-", "--elev-", "--radius-"))
    }
    assert design_token_names
    for path in APP_CSS:
        declared = set(_declarations(path.read_text()))
        assert not design_token_names & declared, path


def test_brand_fonts_are_vendored_and_offline():
    """The UIs are offline: the brand faces ship locally, never from a CDN."""
    css = FONTS_CSS.read_text()
    assert "@font-face" in css
    # Relative src, so the sheet keeps resolving under a mounted base path —
    # static assets get no {{BASE}} substitution.
    assert 'src: url("fonts/' in css

    faces = sorted(p.name for p in FONTS_DIR.glob("*.woff2"))
    assert faces, "no vendored woff2 faces"
    for name in faces:
        assert f'url("fonts/{name}")' in css, f"{name} is vendored but unreferenced"
    for family in ('"Geist"', '"Geist Mono"', '"Inter"'):
        assert f"font-family: {family};" in css

    # The OFL requires the licence to travel with the binaries.
    assert (FONTS_DIR / "LICENSE.md").exists()


def test_no_ui_stylesheet_reaches_for_the_network():
    for path in [TOKENS_CSS, FONTS_CSS, *APP_CSS]:
        text = path.read_text()
        assert "@import url(" not in text, path
        assert "fonts.googleapis.com" not in text, path
        assert "url(http" not in text.replace(" ", ""), path


def test_shared_tokens_live_under_packaged_bobi_tree():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel_packages = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    sdist_include = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    for asset in (TOKENS_CSS, FONTS_CSS, FONTS_DIR):
        assert asset.exists()
        assert asset.is_relative_to(ROOT / "bobi")
    assert "bobi" in wheel_packages
    assert "bobi" in sdist_include
