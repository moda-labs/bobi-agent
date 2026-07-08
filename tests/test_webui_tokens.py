import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent
TOKENS_CSS = ROOT / "bobi" / "webui_common" / "static" / "tokens.css"
APP_CSS = [
    ROOT / "bobi" / "setup" / "webui" / "static" / "app.css",
    ROOT / "bobi" / "webapp" / "static" / "app.css",
]

DESIGN_TOKENS = {
    "--bg": "#F4F1EA",
    "--surface": "#FBFAF6",
    "--raised": "#FFFDF9",
    "--text": "#1F1B16",
    "--muted": "#7A7062",
    "--faint": "#A89E90",
    "--border": "#E2DCD0",
    "--border-strong": "#D6CCBD",
    "--slab-bg": "#181410",
    "--slab-surface": "#1E1A15",
    "--slab-text": "#E8E2D6",
    "--slab-muted": "#948A7B",
    "--slab-border": "#2A241D",
    "--syn-key": "#AFC0D2",
    "--syn-str": "#CBBA8B",
    "--syn-punc": "#7E776B",
    "--syn-com": "#6E6354",
    "--accent": "#C8612B",
    "--accent-2": "#D86E33",
    "--slab-accent": "#E0843F",
}


def _declarations(css: str) -> dict[str, str]:
    return dict(re.findall(r"(?m)^\s*(--[\w-]+)\s*:\s*([^;]+);", css))


def test_shared_tokens_match_design_system_values():
    declarations = _declarations(TOKENS_CSS.read_text())
    for token, value in DESIGN_TOKENS.items():
        assert declarations[token] == value


def test_app_styles_do_not_redeclare_design_tokens():
    design_token_names = set(DESIGN_TOKENS)
    for path in APP_CSS:
        declarations = set(_declarations(path.read_text()))
        assert not design_token_names & declarations, path


def test_shared_tokens_live_under_packaged_bobi_tree():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel_packages = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    sdist_include = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert TOKENS_CSS.exists()
    assert TOKENS_CSS.is_relative_to(ROOT / "bobi")
    assert "bobi" in wheel_packages
    assert "bobi" in sdist_include
