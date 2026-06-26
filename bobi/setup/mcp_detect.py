"""Detect a local MCP server's launch recipe from its project folder.

Point this at a folder on disk and it infers how to start that MCP server over
stdio — the **command**, its **args**, and the **environment variables** it
reads — so the setup UI can prefill the "add a local MCP" form instead of
making the user reverse-engineer it.

This is pure, read-only **static analysis**. We parse `pyproject.toml` /
`package.json` for the entry command and scan the source for env-var
references; we NEVER execute the server or install anything. The output is a
best guess for a human to review and complete (secrets are surfaced by name,
never read from disk).

Output shape (``detect`` → dict):
    {
      "ok": True,
      "name": "substack-mcp",
      "runtime": "python-uv",          # human label for the recipe
      "command": "uv",
      "args": ["run", "--directory", "<abs path>", "substack-mcp"],
      "alt_scripts": ["substack-mcp-capture"],   # other entrypoints found
      "env": [
        {"name": "SUBSTACK_COOKIE", "required": True, "secret": True,
         "hint": "session cookie string"},
        ...
      ],
      "notes": ["..."],               # caveats worth showing the user
    }
or {"ok": False, "error": "..."} when the folder isn't a recognizable project.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

# --- env-var classification ----------------------------------------------

# Names that look like credentials → captured as secrets (.env, masked input),
# never echoed. Substring match on the upper-cased name.
_SECRET_HINTS = ("COOKIE", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY",
                 "API_KEY", "PRIVATE", "CREDENTIAL", "AUTH", "ACCESS_KEY")
# A var that merely POINTS at something (a path/URL/host) isn't itself a secret,
# even when its name contains a secret-ish word (e.g. SUBSTACK_COOKIES_PATH).
_NONSECRET_SUFFIXES = ("_PATH", "_DIR", "_FILE", "_URL", "_URI", "_HOST",
                       "_PORT", "_ENDPOINT")


def _is_secret(name: str) -> bool:
    up = name.upper()
    if up.endswith(_NONSECRET_SUFFIXES):
        return False
    return (any(h in up for h in _SECRET_HINTS)
            or up.endswith("_KEY") or up.endswith("_PAT") or up == "PAT")


# Python: os.environ["X"] read (required — KeyError if absent). The negative
# lookahead skips an assignment `os.environ["X"] = …` (a write) while still
# matching a comparison `os.environ["X"] == …` (a read).
_PY_SUBSCRIPT = re.compile(
    r"""os\.environ\s*\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\](?!\s*=(?!=))""")
# Python: os.environ.get("X"[, default]) / os.getenv("X"[, default]).
_PY_GET = re.compile(
    r"""os\.(?:environ\.get|getenv)\s*\(\s*['"]([A-Z][A-Z0-9_]*)['"]"""
    r"""\s*(?:,\s*(?P<default>[^,)]*))?\)""")
# Node: process.env.X  or  process.env["X"].
_JS_ENV = re.compile(
    r"""process\.env(?:\.([A-Z][A-Z0-9_]*)|\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\])""")

# A default that means "absent" — the var is still effectively required, the
# code just sentinels it and validates (often raises) later. The SUBSTACK_COOKIE
# pattern: os.environ.get("SUBSTACK_COOKIE", "").strip() then raise if empty.
_EMPTY_DEFAULTS = ("", '""', "''", "None")

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist",
              "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "tests",
              "test", "captured-fixtures", "fixtures"}


def _merge(out: dict[str, dict], name: str, *, required: bool,
           secret: bool | None = None) -> None:
    """Record a var, OR-ing `required` across all of its references."""
    cur = out.get(name)
    if cur is None:
        out[name] = {"name": name, "required": required,
                     "secret": _is_secret(name) if secret is None else secret,
                     "hint": ""}
    else:
        cur["required"] = cur["required"] or required


def _scan_python_source(text: str, out: dict[str, dict]) -> None:
    """Regex fallback for unparseable files. Coarser than the AST scanner — it
    can't see `or`-fallbacks or alternative groups, so it errs toward required."""
    for m in _PY_SUBSCRIPT.finditer(text):
        _merge(out, m.group(1), required=True)
    for m in _PY_GET.finditer(text):
        default = (m.group("default") or "").strip()
        required = default == "" or default in _EMPTY_DEFAULTS
        _merge(out, m.group(1), required=required)


def _str_const(node) -> str | None:
    return (node.value if isinstance(node, ast.Constant)
            and isinstance(node.value, str) else None)


def _env_access(node):
    """Classify an env read. Returns (name, kind) where kind is:
    'hard'    — os.environ["X"] or .get/getenv with no default (absence breaks
                immediately) → unconditionally required;
    'soft'    — .get/getenv with an empty/None default (a sentinel the code
                validates later) → required unless it has an `or` fallback or is
                one of several alternatives;
    'default' — .get/getenv with a real default → optional.
    Or None when `node` isn't an env read."""
    if isinstance(node, ast.Subscript):
        v = node.value
        if (isinstance(v, ast.Attribute) and v.attr == "environ"
                and isinstance(v.value, ast.Name) and v.value.id == "os"):
            # Only a READ implies the var must be supplied. `os.environ["X"] = …`
            # (Store) and `del os.environ["X"]` (Del) are the server WRITING its
            # own environment (often from CLI flags) — not a required input.
            if not isinstance(node.ctx, ast.Load):
                return None
            name = _str_const(node.slice)
            return (name, "hard") if name else None
        return None
    if isinstance(node, ast.Call):
        f = node.func
        is_get = (isinstance(f, ast.Attribute) and f.attr == "get"
                  and isinstance(f.value, ast.Attribute)
                  and f.value.attr == "environ"
                  and isinstance(f.value.value, ast.Name)
                  and f.value.value.id == "os")
        is_getenv = (isinstance(f, ast.Attribute) and f.attr == "getenv"
                     and isinstance(f.value, ast.Name) and f.value.id == "os")
        if not (is_get or is_getenv) or not node.args:
            return None
        name = _str_const(node.args[0])
        if not name:
            return None
        if len(node.args) < 2:
            return (name, "hard")
        d = node.args[1]
        if isinstance(d, ast.Constant) and d.value in ("", None):
            return (name, "soft")
        return (name, "default")
    return None


def _or_fallback_ids(tree) -> set[int]:
    """ids of nodes that sit on the left of an `a or b` — i.e. have a fallback
    after them, so a missing value isn't fatal (`os.getenv("X") or "default"`)."""
    ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            for operand in n.values[:-1]:
                for sub in ast.walk(operand):
                    ids.add(id(sub))
    return ids


def _scan_python_ast(tree, out: dict[str, dict], notes: list[str]) -> None:
    or_left = _or_fallback_ids(tree)

    # 1) Per-reference classification.
    for node in ast.walk(tree):
        acc = _env_access(node)
        if not acc:
            continue
        name, kind = acc
        if kind == "hard":
            required = True
        elif kind == "soft":
            required = id(node) not in or_left   # `or`-fallback → optional
        else:
            required = False
        _merge(out, name, required=required)

    # 2) Alternatives: a function that raises while reading several SOFT-default
    #    vars is usually "provide one of these" (e.g. cookie string OR a path to
    #    one). Keep the first required; demote the rest and note the choice.
    #    Hard reads are never demoted — they're independently required.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(isinstance(n, ast.Raise) for n in ast.walk(fn)):
            continue
        soft: list[tuple[int, int, str]] = []
        for n in ast.walk(fn):
            acc = _env_access(n)
            if acc and acc[1] == "soft" and id(n) not in or_left:
                soft.append((getattr(n, "lineno", 0),
                             getattr(n, "col_offset", 0), acc[0]))
        soft.sort()
        ordered: list[str] = []
        for _, _, name in soft:
            if name not in ordered:
                ordered.append(name)
        if len(ordered) >= 2:
            for alt in ordered[1:]:
                if alt in out:
                    out[alt]["required"] = False
            note = " or ".join(ordered) + " — provide one (alternatives)."
            if note not in notes:
                notes.append(note)


def _scan_python_file(text: str, out: dict[str, dict],
                      notes: list[str]) -> None:
    """Classify env reads in one Python file — AST when it parses (sees
    `or`-fallbacks and alternative groups), regex otherwise."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        _scan_python_source(text, out)
        return
    _scan_python_ast(tree, out, notes)


def _scan_js_source(text: str, out: dict[str, dict]) -> None:
    for m in _JS_ENV.finditer(text):
        name = m.group(1) or m.group(2)
        tail = text[m.end():].lstrip()
        # `process.env.X = …` is a write, not a required input — skip it (but
        # keep `process.env.X === …`, a comparison/read).
        if tail.startswith("=") and not tail.startswith("=="):
            continue
        # JS rarely encodes required-ness statically; surface it and let the
        # user decide. A `|| "default"` / `?? "default"` fallback → optional.
        optional = tail.startswith("||") or tail.startswith("??")
        _merge(out, name, required=not optional)


# Cap traversal so pointing the scanner at a huge tree (or `~`) can't pin a
# worker reading thousands of files — a server's source is small.
_MAX_SCAN_FILES = 2000


def _iter_source(root: Path, suffixes: tuple[str, ...], exclude: set[Path],
                 skip: set[str] = _SKIP_DIRS):
    seen = 0
    for p in sorted(root.rglob("*")):
        if seen >= _MAX_SCAN_FILES:
            break
        if p.is_symlink() or not p.is_file() or p.suffix not in suffixes:
            continue
        if any(part in skip for part in p.relative_to(root).parts[:-1]):
            continue
        if p.resolve() in exclude:
            continue
        seen += 1
        yield p


def _parse_env_example(folder: Path, out: dict[str, dict]) -> None:
    """Merge any .env.example / .env.sample — names (and a hint from the inline
    comment or the example value). Never reads a real .env (secrets)."""
    for fname in (".env.example", ".env.sample", ".env.template",
                  "env.example"):
        f = folder / fname
        if not f.is_file():
            continue
        for raw in f.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip().lstrip("export ").strip()
            if not re.match(r"^[A-Z][A-Z0-9_]*$", name):
                continue
            _merge(out, name, required=True)


def _readme_text(folder: Path) -> str:
    for readme in ("README.md", "README.rst", "README.txt", "README"):
        f = folder / readme
        if f.is_file():
            return f.read_text(errors="ignore")
    return ""


def _clean_hint(line: str, name: str) -> str:
    """A terse one-line description from a README/doc line mentioning `name` —
    with the var name, shell/markdown noise, and any inline value stripped."""
    h = line.replace(name, " ")
    h = re.sub(r"\bexport\b", " ", h)
    # Drop an inline assignment / example value: `="https://…"`, `=foo`, etc.
    h = re.sub(r"=\s*\S+", " ", h)
    h = re.sub(r"\s+", " ", h).strip(" \t|`*-:#>=\"'.,")
    return h[:120].strip()


def _attach_hints(folder: Path, out: dict[str, dict]) -> None:
    """A one-line description per var, pulled from the first README line that
    mentions it (cheap, best-effort — skipped when no README)."""
    text = _readme_text(folder)
    if not text:
        return
    for name, rec in out.items():
        if rec["hint"]:
            continue
        for line in text.splitlines():
            if name in line:
                h = _clean_hint(line, name)
                if h:
                    rec["hint"] = h
                break


def _harvest_readme_vars(folder: Path, out: dict[str, dict]) -> None:
    """Catch config vars the source scan misses because the code reads them
    indirectly (e.g. `os.environ.get(name, ...)` through a helper). Only adds
    README tokens that SHARE A PREFIX with an already-detected var, so this
    stays low-false-positive. README-only vars are treated as optional."""
    if not out:
        return
    text = _readme_text(folder)
    if not text:
        return
    prefixes = {n.split("_", 1)[0] + "_" for n in out if "_" in n}
    if not prefixes:
        return
    for tok in set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text)):
        if tok in out:
            continue
        if any(tok.startswith(p) and tok != p.rstrip("_") for p in prefixes):
            _merge(out, tok, required=False)
            for line in text.splitlines():
                if tok in line:
                    out[tok]["hint"] = _clean_hint(line, tok)
                    break


# Above this many "required" vars, the required/optional split is no longer
# trustworthy — a server this configurable usually needs only a SUBSET (one of
# several products / auth modes), which static analysis can't pick.
_MANY_REQUIRED = 6


def _apply_confidence_guard(out: dict[str, dict], notes: list[str]) -> None:
    """When too many vars look required, stop asserting it. A multi-product /
    multi-auth server (e.g. Jira+Confluence × token/PAT/OAuth) legitimately
    reads dozens of vars but needs only a handful — claiming all are required is
    worse than honest uncertainty. Demote to optional and say so; secret flags
    (for masked input) are kept."""
    required = [r for r in out.values() if r["required"]]
    if len(required) <= _MANY_REQUIRED:
        return
    for r in out.values():
        r["required"] = False
    notes.append(
        f"{len(out)} environment variables detected, many of which looked "
        f"required — a server this configurable usually needs only a subset "
        f"(e.g. for your chosen product or auth method). Treating them as "
        f"optional; set the ones that apply (see the server's README).")


def _env_list(out: dict[str, dict]) -> list[dict]:
    """Stable order: required-first, then secrets, then alphabetical."""
    return sorted(out.values(),
                  key=lambda r: (not r["required"], not r["secret"], r["name"]))


# --- per-runtime detection -----------------------------------------------

def _choose_python_script(name: str, scripts: dict[str, str]) -> str:
    """Pick the server entrypoint among a project's console scripts. Prefer the
    one matching the project name, then a 'server'/'mcp' script, avoiding
    obvious side tools (capture/dump/test/dev)."""
    if not scripts:
        return ""
    if name in scripts:
        return name
    avoid = ("capture", "dump", "export", "test", "dev", "lint", "fmt")
    ranked = sorted(
        scripts,
        key=lambda s: (any(a in s.lower() for a in avoid),
                       not ("server" in s.lower() or "mcp" in s.lower()), s))
    return ranked[0]


def _detect_python(folder: Path, pyproject: Path) -> dict:
    try:
        data = tomllib.loads(pyproject.read_text(errors="ignore"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        return {"ok": False, "error": f"could not read pyproject.toml: {e}"}
    project = data.get("project") or {}
    name = project.get("name") or folder.name
    scripts = project.get("scripts") or {}
    notes: list[str] = []

    chosen = _choose_python_script(name, scripts)
    if chosen:
        args = ["run", "--directory", str(folder), chosen]
        alt = [s for s in scripts if s != chosen]
    else:
        # No console script — fall back to `python -m <package>` if there's an
        # importable package with a __main__, else flag it for manual entry.
        pkg = _guess_python_package(folder)
        if pkg:
            args = ["run", "--directory", str(folder), "python", "-m", pkg]
            notes.append(f"No console script declared; guessed `python -m "
                         f"{pkg}`. Verify the module is runnable.")
        else:
            args = ["run", "--directory", str(folder), "python", "-m",
                    "<module>"]
            notes.append("No console script or runnable package found — set "
                         "the command/args manually.")
        alt = []

    env: dict[str, dict] = {}
    src_root = folder / "src" if (folder / "src").is_dir() else folder
    exclude = _excluded_files(folder, src_root, scripts, chosen)
    for p in _iter_source(src_root, (".py",), exclude):
        _scan_python_file(p.read_text(errors="ignore"), env, notes)
    _parse_env_example(folder, env)
    _attach_hints(folder, env)
    _harvest_readme_vars(folder, env)
    _apply_confidence_guard(env, notes)

    return {"ok": True, "name": name, "runtime": "python-uv",
            "command": "uv", "args": args, "alt_scripts": alt,
            "env": _env_list(env), "notes": notes}


def _guess_python_package(folder: Path) -> str:
    src = folder / "src"
    search = src if src.is_dir() else folder
    for child in sorted(search.iterdir()):
        if child.is_dir() and (child / "__init__.py").is_file() \
                and child.name not in _SKIP_DIRS:
            if (child / "__main__.py").is_file():
                return child.name
    # Any package at all, even without __main__ — caller flags it as a guess.
    for child in sorted(search.iterdir()):
        if child.is_dir() and (child / "__init__.py").is_file() \
                and child.name not in _SKIP_DIRS:
            return child.name
    return ""


def _excluded_files(folder: Path, src_root: Path, scripts: dict[str, str],
                    chosen: str) -> set[Path]:
    """Files belonging to OTHER console scripts' entry modules — their env vars
    aren't the chosen server's. Maps `pkg.mod:fn` → pkg/mod.py under src_root."""
    excl: set[Path] = set()
    for sname, target in scripts.items():
        if sname == chosen:
            continue
        module = str(target).split(":", 1)[0].strip()
        if not module:
            continue
        rel = Path(*module.split(".")).with_suffix(".py")
        for base in (src_root, folder):
            cand = base / rel
            if cand.is_file():
                excl.add(cand.resolve())
    return excl


def _detect_node(folder: Path, pkgjson: Path) -> dict:
    try:
        data = json.loads(pkgjson.read_text(errors="ignore"))
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "error": f"could not read package.json: {e}"}
    name = data.get("name") or folder.name
    notes: list[str] = []
    bin_field = data.get("bin")
    main = data.get("main")
    if isinstance(bin_field, str):
        entry = bin_field
    elif isinstance(bin_field, dict) and bin_field:
        entry = bin_field.get(name) or next(iter(bin_field.values()))
    elif main:
        entry = main
        notes.append("No `bin` entry; using package `main`. Verify it starts "
                     "the server.")
    else:
        entry = "<entry.js>"
        notes.append("No `bin` or `main` in package.json — set the command/args "
                     "manually.")
    args = [str((folder / entry))] if entry and not entry.startswith("<") \
        else [entry]

    env: dict[str, dict] = {}
    # Node entrypoints often live in dist/build, so don't skip those here (but
    # still skip node_modules — huge and not the server's own code).
    node_skip = _SKIP_DIRS - {"dist", "build"}
    for p in _iter_source(folder, (".js", ".mjs", ".cjs", ".ts"), set(),
                          skip=node_skip):
        _scan_js_source(p.read_text(errors="ignore"), env)
    _parse_env_example(folder, env)
    _attach_hints(folder, env)
    _harvest_readme_vars(folder, env)
    _apply_confidence_guard(env, notes)

    return {"ok": True, "name": name, "runtime": "node",
            "command": "node", "args": args, "alt_scripts": [],
            "env": _env_list(env), "notes": notes}


# --- entry point ----------------------------------------------------------

def _clean_path_input(raw: str) -> str:
    """Normalize a pasted folder path. Users copy paths from Finder/terminal,
    which often arrive wrapped in quotes ("…"/'…') or with shell-escaped spaces
    (/Moda\\ Labs/…) — strip one layer of matching quotes and unescape spaces so
    those paste straight in."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def detect(path) -> dict:
    """Infer a stdio launch recipe from a local MCP project folder. `path` may
    point at the project root or a file inside it; we resolve to the nearest
    folder holding a pyproject.toml / package.json."""
    raw = _clean_path_input(str(path))
    folder = Path(raw).expanduser()
    # Fall back to an unescaped form if the literal path doesn't exist — drag &
    # drop / shell copy escapes spaces and other chars with a backslash.
    if not folder.exists() and "\\" in raw:
        folder = Path(re.sub(r"\\(.)", r"\1", raw)).expanduser()
    if not folder.exists():
        return {"ok": False, "error": f"no such folder: {folder}"}
    if folder.is_file():
        folder = folder.parent
    if not folder.is_dir():
        return {"ok": False, "error": f"not a folder: {folder}"}
    folder = folder.resolve()
    # Walk up a few levels in case they pointed at a subdir (src/, etc.).
    for cand in (folder, *folder.parents[:3]):
        if (cand / "pyproject.toml").is_file():
            return _detect_python(cand, cand / "pyproject.toml")
        if (cand / "package.json").is_file():
            return _detect_node(cand, cand / "package.json")
    return {"ok": False,
            "error": "no pyproject.toml or package.json found here — point at "
                     "the MCP server's project root."}
