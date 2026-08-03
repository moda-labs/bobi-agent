"""The webapp's agent-reply markdown renderer, executed as real JavaScript.

`bobi/webapp/static/views/markdown.js` turns agent output into HTML that the
dashboard assigns with innerHTML, so its escaping is the only thing between a
prompt-injected agent reply and script execution in the operator's origin.
Asserting on the JS source text would prove nothing about what it renders, so
these run the actual module under Node and parse what comes back.

Node is already a hard requirement of this repo's build
(hatch_build.py::_require_build_node), so this adds no dependency.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "bobi" / "webapp" / "static" / "views" / "markdown.js"

# The published payload for D008: no space, no paren, no `)` — every character
# the link regex's URL group forbids — so it survives into the href attribute.
XSS_PAYLOAD = '[click](https://example.com"onmouseover="location=document.cookie//)'


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        # Never skip on CI: a silently skipped security test is a green that
        # proves nothing (the lesson of this repo's vacuous live lanes).
        if os.environ.get("CI"):
            pytest.fail("node is required to run the webapp markdown tests")
        pytest.skip("node not on PATH")
    return node


def render(src: str) -> str:
    """Render `src` through the real module and return the HTML."""
    script = (
        "const { renderMarkdown } = await import(process.argv[1]);"
        "process.stdout.write(renderMarkdown(process.argv[2]));"
    )
    proc = subprocess.run(
        [_node(), "--input-type=module", "-e", script, MODULE.as_uri(), src],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout


class _Attrs(HTMLParser):
    """Collect every (tag, attrs) the way a browser's parser would see it."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def parse(html: str) -> list[tuple[str, dict[str, str | None]]]:
    p = _Attrs()
    p.feed(html)
    return p.tags


def test_module_exists() -> None:
    assert MODULE.is_file(), f"missing {MODULE}"


def test_link_url_cannot_inject_an_event_handler_attribute() -> None:
    """D008 — a quote in the URL must not escape the href attribute.

    An unescaped `"` closes href early; HTML parsers (browsers included, and
    Python's, as this test relies on) recover by reading the rest as further
    attributes, so the reply lands a live `onmouseover` in a page with no CSP.
    """
    tags = parse(render(XSS_PAYLOAD))
    anchors = [attrs for tag, attrs in tags if tag == "a"]
    assert anchors, "expected the payload to still render as a link"
    for attrs in anchors:
        handlers = [k for k in attrs if k.startswith("on")]
        assert not handlers, f"agent output injected event handlers: {handlers}"
    # The whole payload stays inside the single href value.
    assert anchors[0]["href"] is not None
    assert "onmouseover" in anchors[0]["href"]


def test_quotes_in_prose_survive_as_text() -> None:
    """Escaping quotes must not corrupt ordinary text."""
    out = render('He said "hello" and it\'s fine.')
    assert '"hello"' in out.replace("&quot;", '"').replace("&#39;", "'")
    assert not [k for _, attrs in parse(out) for k in attrs if k.startswith("on")]


def test_ordinary_link_still_renders() -> None:
    tags = parse(render("see [docs](https://example.com/a?x=1&y=2)"))
    anchors = [attrs for tag, attrs in tags if tag == "a"]
    assert len(anchors) == 1
    assert anchors[0]["href"] == "https://example.com/a?x=1&y=2"
    assert anchors[0]["rel"] == "noopener noreferrer"


def test_non_http_scheme_is_neutralized() -> None:
    """The scheme allowlist (already shipped) keeps javascript: URLs out."""
    tags = parse(render("[x](javascript:alert%601%60)"))
    anchors = [attrs for tag, attrs in tags if tag == "a"]
    assert len(anchors) == 1
    assert anchors[0]["href"] == "#"


def test_markup_in_agent_output_is_escaped() -> None:
    out = render("<img src=x onerror=alert(1)>")
    assert "<img" not in out
    assert not [k for _, attrs in parse(out) for k in attrs if k.startswith("on")]


def test_code_fence_and_inline_code_still_render() -> None:
    out = render("run `bobi app start`\n\n```\nline one\nline two\n```\n")
    assert "<code>bobi app start</code>" in out
    assert "<pre><code>line one\nline two</code></pre>" in out


def test_headings_lists_and_emphasis_still_render() -> None:
    out = render("# Title\n\n- one\n- two\n\n**bold** and *em*\n")
    tag_names = [t for t, _ in parse(out)]
    assert "h3" in tag_names  # h1 renders as h3 (min(level+2, 6))
    assert tag_names.count("li") == 2
    assert "<strong>bold</strong>" in out
    assert "<em>em</em>" in out


def test_renderer_is_not_duplicated_back_into_the_view() -> None:
    """Q025 — the renderer stays a module, not a closure in the view file.

    It is security-critical and only testable while it is importable; a copy
    pasted back into agent.js would silently escape this suite.
    """
    view = MODULE.parent / "agent.js"
    src = view.read_text()
    assert "function renderMarkdown" not in src
    assert 'from "./markdown.js"' in src


def test_json_roundtrip_payload_is_what_we_think_it_is() -> None:
    """Guard the payload itself: no `)`, no whitespace inside the URL."""
    url = XSS_PAYLOAD.split("(", 1)[1].rsplit(")", 1)[0]
    assert ")" not in url and not any(c.isspace() for c in url), json.dumps(url)
