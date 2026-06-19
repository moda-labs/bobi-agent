"""Folder-scan detection of a local stdio MCP server's launch recipe."""

import textwrap

import pytest

from modastack.setup import mcp_detect


def _write(folder, rel, text):
    p = folder / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))
    return p


@pytest.fixture
def py_project(tmp_path):
    """A realistic uv/pyproject MCP server: a console script, a config module
    that reads env vars (required-via-raise + optional-with-default), a second
    'capture' script whose vars must NOT leak, and a README documenting an
    indirectly-read var."""
    _write(tmp_path, "pyproject.toml", """
        [project]
        name = "acme-mcp"
        version = "0.1.0"

        [project.scripts]
        acme-mcp = "acme_mcp.server:main"
        acme-mcp-capture = "acme_mcp.capture:main"
    """)
    _write(tmp_path, "src/acme_mcp/__init__.py", "")
    _write(tmp_path, "src/acme_mcp/server.py", "def main():\n    pass\n")
    _write(tmp_path, "src/acme_mcp/config.py", """
        import os

        def load():
            token = os.environ.get("ACME_TOKEN", "").strip()
            if not token:
                raise RuntimeError("need ACME_TOKEN")
            base = os.environ["ACME_BASE_URL"]
            ua = os.environ.get("ACME_USER_AGENT", "acme/1.0")
            return token, base, ua
    """)
    _write(tmp_path, "src/acme_mcp/capture.py", """
        import os
        OUT = os.environ.get("ACME_CAPTURE_DIR", "/tmp")
    """)
    _write(tmp_path, "README.md", """
        # acme-mcp

        | Var | Default | Meaning |
        | ACME_TIMEOUT | 30 | Per-request timeout (s) |
    """)
    return tmp_path


class TestPythonDetection:
    def test_command_uses_uv_run_directory_and_chosen_script(self, py_project):
        d = mcp_detect.detect(str(py_project))
        assert d["ok"] is True
        assert d["runtime"] == "python-uv"
        assert d["command"] == "uv"
        assert d["args"][:3] == ["run", "--directory", str(py_project)]
        # The script matching the project name is chosen; capture is an alt.
        assert d["args"][-1] == "acme-mcp"
        assert d["alt_scripts"] == ["acme-mcp-capture"]

    def test_required_secret_classification(self, py_project):
        env = {e["name"]: e for e in mcp_detect.detect(str(py_project))["env"]}
        # get("X","") + raise → required; subscript → required.
        assert env["ACME_TOKEN"]["required"] is True
        assert env["ACME_TOKEN"]["secret"] is True
        assert env["ACME_BASE_URL"]["required"] is True
        assert env["ACME_BASE_URL"]["secret"] is False   # _URL → not a secret
        # get with a real default → optional.
        assert env["ACME_USER_AGENT"]["required"] is False

    def test_other_scripts_env_vars_excluded(self, py_project):
        names = {e["name"] for e in mcp_detect.detect(str(py_project))["env"]}
        # ACME_CAPTURE_DIR lives in the capture script's module → not the server's.
        assert "ACME_CAPTURE_DIR" not in names

    def test_readme_harvest_catches_indirect_var(self, py_project):
        env = {e["name"]: e for e in mcp_detect.detect(str(py_project))["env"]}
        # ACME_TIMEOUT is only in the README (read indirectly in real code).
        assert "ACME_TIMEOUT" in env
        assert env["ACME_TIMEOUT"]["required"] is False

    def test_env_order_required_secret_first(self, py_project):
        env = mcp_detect.detect(str(py_project))["env"]
        # The required secret sorts ahead of optional/plain vars.
        assert env[0]["name"] == "ACME_TOKEN"

    def test_accepts_a_path_inside_the_project(self, py_project):
        d = mcp_detect.detect(str(py_project / "src" / "acme_mcp"))
        assert d["ok"] is True and d["name"] == "acme-mcp"

    @pytest.mark.parametrize("wrap", ['"{}"', "'{}'", " {} ", "  '{}'  "])
    def test_accepts_quoted_and_padded_paths(self, py_project, wrap):
        d = mcp_detect.detect(wrap.format(py_project))
        assert d["ok"] is True and d["name"] == "acme-mcp"

    def test_accepts_backslash_escaped_spaces(self, tmp_path):
        # Drag-and-drop / shell copy escapes spaces: /a/Moda\ Labs/proj
        proj = tmp_path / "Moda Labs" / "proj"
        proj.mkdir(parents=True)
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "spacey"\nversion = "0.1.0"\n')
        escaped = str(proj).replace(" ", "\\ ")
        d = mcp_detect.detect(escaped)
        assert d["ok"] is True and d["name"] == "spacey"

    def test_or_fallback_marks_optional(self, tmp_path):
        # `os.environ.get("X", "") or None` is NOT required — it has a fallback.
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "orfb"\nversion = "0.1.0"\n'
               '[project.scripts]\norfb = "orfb.server:main"\n')
        _write(tmp_path, "src/orfb/__init__.py", "")
        _write(tmp_path, "src/orfb/server.py", """
            import os
            def main():
                pub = os.environ.get("ORFB_PUBLICATION_URL", "").strip() or None
                key = os.environ.get("ORFB_KEY", "")
                if not key:
                    raise RuntimeError("need key")
        """)
        env = {e["name"]: e for e in mcp_detect.detect(str(tmp_path))["env"]}
        assert env["ORFB_PUBLICATION_URL"]["required"] is False
        assert env["ORFB_KEY"]["required"] is True

    def test_alternatives_demoted_with_note(self, tmp_path):
        # Two soft-default vars resolved in a raising function → "provide one".
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "alt"\nversion = "0.1.0"\n'
               '[project.scripts]\nalt = "alt.server:main"\n')
        _write(tmp_path, "src/alt/__init__.py", "")
        _write(tmp_path, "src/alt/server.py", """
            import os
            def resolve():
                a = os.environ.get("ALT_COOKIE", "").strip()
                if a:
                    return a
                b = os.environ.get("ALT_COOKIES_PATH", "").strip()
                if b:
                    return b
                raise RuntimeError("need one")
        """)
        d = mcp_detect.detect(str(tmp_path))
        env = {e["name"]: e for e in d["env"]}
        assert env["ALT_COOKIE"]["required"] is True       # first stays required
        assert env["ALT_COOKIES_PATH"]["required"] is False  # alternative demoted
        assert any("provide one" in n for n in d["notes"])

    def test_hard_subscript_never_demoted(self, tmp_path):
        # A required subscript read alongside a soft var must NOT be treated as
        # an alternative — both are independently required.
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "hard"\nversion = "0.1.0"\n'
               '[project.scripts]\nhard = "hard.server:main"\n')
        _write(tmp_path, "src/hard/__init__.py", "")
        _write(tmp_path, "src/hard/server.py", """
            import os
            def main():
                tok = os.environ.get("HARD_TOKEN", "")
                if not tok:
                    raise RuntimeError("need token")
                base = os.environ["HARD_BASE_URL"]
        """)
        env = {e["name"]: e for e in mcp_detect.detect(str(tmp_path))["env"]}
        assert env["HARD_TOKEN"]["required"] is True
        assert env["HARD_BASE_URL"]["required"] is True

    def test_assignment_target_is_not_a_required_read(self, tmp_path):
        # `os.environ["X"] = ...` is the server WRITING its env (often from CLI
        # flags) — it must NOT be counted as a required input. A comparison
        # (`== ...`) and a plain read still count.
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "wr"\nversion = "0.1.0"\n'
               '[project.scripts]\nwr = "wr.server:main"\n')
        _write(tmp_path, "src/wr/__init__.py", "")
        _write(tmp_path, "src/wr/server.py", """
            import os
            def main():
                os.environ["WR_WRITTEN"] = "x"          # write — not required
                host = os.environ["WR_READ"]            # read — required
                if os.environ["WR_COMPARED"] == "y":    # comparison — read
                    pass
        """)
        env = {e["name"]: e for e in mcp_detect.detect(str(tmp_path))["env"]}
        assert "WR_WRITTEN" not in env                  # write-only → absent
        assert env["WR_READ"]["required"] is True
        assert env["WR_COMPARED"]["required"] is True

    def test_regex_fallback_skips_assignment(self):
        # The regex path (used when a file won't parse) honors the same rule.
        out = {}
        mcp_detect._scan_python_source(
            'os.environ["A"] = 1\nx = os.environ["B"]\n'
            'if os.environ["C"] == 2: pass\n', out)
        assert "A" not in out
        assert out["B"]["required"] is True
        assert out["C"]["required"] is True

    def test_confidence_guard_demotes_when_many_required(self, tmp_path):
        # A highly configurable server (many required-looking vars) can't be
        # statically split into required/optional → demote all + note.
        reads = "\n".join(f'    v{i} = os.environ["BIG_VAR_{i}"]'
                          for i in range(9))
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "big"\nversion = "0.1.0"\n'
               '[project.scripts]\nbig = "big.server:main"\n')
        _write(tmp_path, "src/big/__init__.py", "")
        _write(tmp_path, "src/big/server.py",
               "import os\ndef main():\n" + reads + "\n")
        d = mcp_detect.detect(str(tmp_path))
        assert len(d["env"]) == 9
        assert all(not e["required"] for e in d["env"])     # all demoted
        assert any("subset" in n for n in d["notes"])
        # Secret flags survive the demotion (so masked input still applies).
        assert all("secret" in e for e in d["env"])

    def test_small_server_keeps_required_flags(self, tmp_path):
        # Below the threshold, precise classification is preserved.
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "sm"\nversion = "0.1.0"\n'
               '[project.scripts]\nsm = "sm.server:main"\n')
        _write(tmp_path, "src/sm/__init__.py", "")
        _write(tmp_path, "src/sm/server.py", """
            import os
            def main():
                a = os.environ["SM_A"]
                b = os.environ["SM_B"]
        """)
        env = {e["name"]: e for e in mcp_detect.detect(str(tmp_path))["env"]}
        assert env["SM_A"]["required"] and env["SM_B"]["required"]

    def test_js_assignment_skipped(self, tmp_path):
        _write(tmp_path, "package.json", '{"name":"jsw","bin":"index.js"}')
        _write(tmp_path, "index.js", """
            process.env.JSW_WRITTEN = "x";
            const r = process.env.JSW_READ;
            if (process.env.JSW_CMP === "y") {}
        """)
        env = {e["name"]: e for e in mcp_detect.detect(str(tmp_path))["env"]}
        assert "JSW_WRITTEN" not in env
        assert "JSW_READ" in env and "JSW_CMP" in env

    def test_module_fallback_when_no_console_script(self, tmp_path):
        _write(tmp_path, "pyproject.toml",
               '[project]\nname = "noscript"\nversion = "0.1.0"\n')
        _write(tmp_path, "src/noscript/__init__.py", "")
        _write(tmp_path, "src/noscript/__main__.py", "")
        d = mcp_detect.detect(str(tmp_path))
        assert d["args"][-2:] == ["-m", "noscript"]
        assert d["notes"]   # flags the guess


class TestNodeDetection:
    def test_bin_entry_and_process_env(self, tmp_path):
        _write(tmp_path, "package.json",
               '{"name":"acme-node-mcp","bin":"dist/index.js"}')
        _write(tmp_path, "dist/index.js", """
            const k = process.env.ACME_API_KEY;
            const region = process.env.ACME_REGION || "us";
        """)
        d = mcp_detect.detect(str(tmp_path))
        assert d["ok"] is True and d["runtime"] == "node"
        assert d["command"] == "node"
        assert d["args"] == [str(tmp_path / "dist/index.js")]
        env = {e["name"]: e for e in d["env"]}
        assert env["ACME_API_KEY"]["secret"] is True
        # `|| "us"` fallback → optional.
        assert env["ACME_REGION"]["required"] is False


class TestErrors:
    def test_missing_folder(self, tmp_path):
        d = mcp_detect.detect(str(tmp_path / "nope"))
        assert d["ok"] is False and "no such folder" in d["error"]

    def test_unrecognized_project(self, tmp_path):
        (tmp_path / "random.txt").write_text("hi")
        d = mcp_detect.detect(str(tmp_path))
        assert d["ok"] is False and "pyproject" in d["error"]
