"""Differential tests: the shared redactor never keeps a letter or digit that
the merge base's redactor removes (quote characters around a redacted value
may remain).

The reference (``_main_redact_reference``) is the merge base's
``redact_secrets`` copied verbatim. With credential values, the reference
scrubs every occurrence of the values first and then runs that redactor, once
per value and once with all values together. A generated corpus compares the
characters each side removes: no letter or digit that a reference removes may
survive, and no character of a credential value may.

``BOBI_REDACTION_CORPUS`` sets the corpus size (default 100,000 strings).
"""

import base64
import os
import random
import re
import time

import pytest

from bobi.metrics.features import prepare_task
from bobi.redact import (REDACTION_PLACEHOLDER, redact_secrets, redact_with_values, secret_spans,
                         secret_spans_with_values)
from tests.metrics import _main_redact_reference as reference

CORPUS_SIZE = int(os.environ.get("BOBI_REDACTION_CORPUS", "100000"))
SEED = 20261014

_LETTER_OR_DIGIT = re.compile(r"[^\W_]+")
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_UPPER_DIGITS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_KEYS = ["password", "Password", "PASSWORD", "passwd", "pwd", "secret", "Secret", "api_key", "api-key",
         "apikey", "ApiKey", "API_KEY", "Api-Key", "access_token", "auth-token", "refresh_token",
         "client_secret", "bot_token", "secret_key", "private_key", "db.password", "DB_PASSWORD",
         "GH_TOKEN", "x-password", "my.secret", "--password", "PGPASSWORD", "DB_PASS", "token", "key"]
_SEPARATORS = ["=", ": ", " = ", ":", "=\n", ":\t", "= ", " :"]
_GLUES = ["", ":", "/", "+", "&", "-", "_", ".", "=", "@", ";", " ", ",", "'", '"']
_TAILS = ["", "", "", "&password=hunter2", ";ApiKey=hunter2", ":fakefake1234", "/fakeFAKE1234+tailTAIL99==",
          ',"next": "value9"}', "'", '"', ")", ".", "?x=1", "@db.example.com/prod", "abcd1234", " tail9word"]
_WORDS = ["deploy", "review", "the", "branch", "task-implement-fine-grained-metrics", "postgres", "key", "token",
          "basic", "bearer", "Server", "host", "user", "admin", "redacted", "[redacted]", "café", "naïve9"]
_SCHEMES = ["postgres", "postgresql", "mysql", "https", "redis", "amqp", "mongodb+srv"]


def _run(rng, alphabet, low, high):
    return "".join(rng.choices(alphabet, k=rng.randint(low, high)))


def _token(rng):
    kind = rng.randrange(16)
    if kind == 0:
        return "sk-" + _run(rng, _ALNUM, 20, 30)
    if kind == 1:
        return "sk-" + _run(rng, _ALNUM + "-_", 20, 30)
    if kind == 2:
        return "sk-ant-" + _run(rng, _ALNUM + "-_", 20, 30)
    if kind == 3:
        return "AKIA" + _run(rng, _UPPER_DIGITS, 16, 16)
    if kind == 4:
        return "ghp_" + _run(rng, _ALNUM, 20, 36)
    if kind == 5:
        return "xoxb-" + _run(rng, _ALNUM + "-", 10, 30)
    if kind == 6:
        return "AIza" + _run(rng, _ALNUM + "_-", 20, 35)
    if kind == 7:
        return "eyJ" + _run(rng, _ALNUM, 20, 30) + "." + _run(rng, _ALNUM, 5, 20) + "." + _run(rng, _ALNUM, 5, 20)
    if kind == 8:
        return base64.b64encode(rng.randbytes(rng.choice([12, 24, 48, 64, 96]))).decode()
    if kind == 9:
        return _run(rng, "0123456789abcdef", 32, 64)
    if kind == 10:
        return _run(rng, _ALNUM + "_-", 40, 56)
    if kind == 11:
        return "[redacted]" + _run(rng, _ALNUM, 4, 12)
    if kind == 12:
        return "my pass phrase " + _run(rng, _ALNUM, 4, 8)
    if kind == 13:
        return "pässwört" + _run(rng, _ALNUM, 2, 6)
    if kind == 14:
        return "task-" + _run(rng, _ALNUM, 20, 26)
    return _run(rng, _ALNUM, 4, 14)


def _value(rng):
    value = _token(rng)
    for _ in range(rng.choice([0, 0, 1, 1, 2])):
        value += rng.choice(_GLUES) + _token(rng)
    return value


def _fragment(rng, pieces):
    """One fragment of a generated text; credential-like parts go to *pieces*."""
    key, key2 = rng.choice(_KEYS), rng.choice(_KEYS)
    value, value2 = _value(rng), _value(rng)
    pieces += [value, value2]
    tail = rng.choice(_TAILS)
    kind = rng.randrange(14)
    if kind <= 2:
        return f"{key}{rng.choice(_SEPARATORS)}{value}{tail}"
    if kind == 3:
        quote = rng.choice(['"', "'"])
        return f"{key}{rng.choice(_SEPARATORS)}{quote}{value}{quote}{tail}"
    if kind == 4:
        quote = rng.choice(['"""', "'''"])
        return f"{key} = {quote}{value}\n{value2}{quote}{tail}"
    if kind == 5:
        return f'{key}="a\\"{value}"{tail}'
    if kind == 6:
        bare = rng.random() < 0.3
        shown = value if bare else f'"{value}"'
        return f'{{"{key}": {shown}, "user": "{rng.choice(_WORDS)}"}}{tail}'
    if kind == 7:
        return f"https://api.example.com/v1/x?{key}={value}&{key2}={value2}{tail}"
    if kind == 8:
        return f"Server=db;{key.title()}={value};{key2}={value2}{tail}"
    if kind == 9:
        user = rng.choice(["u", "admin", _run(rng, _ALNUM, 3, 8), value2])
        return f"{rng.choice(_SCHEMES)}://{user}:{value}@db.example.com/prod{tail}"
    if kind == 10:
        scheme = rng.choice(["Authorization: Bearer ", "authorization: bearer ", "Authorization: Basic ",
                             "authorization:basic ", "Bearer "])
        return f"{scheme}{value}{tail}"
    if kind == 11:
        return f"{rng.choice(_WORDS)}{_token(rng)}{rng.choice(_GLUES)}{rng.choice(_WORDS)}{tail}"
    if kind == 12:
        return f"{key}{rng.choice(_SEPARATORS)}[redacted]{tail}{rng.choice(_GLUES)}{value}"
    return " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 5)))


def _env_value(rng, text, pieces):
    kind = rng.randrange(9)
    if kind <= 1:
        return rng.choice(pieces)
    if kind == 2:
        piece = rng.choice(pieces)
        start = rng.randrange(len(piece))
        return piece[start:start + rng.randint(3, 10)]
    if kind == 3:
        return rng.choice(_KEYS + ["Server", "Authorization", "Bearer", "Basic"])
    if kind == 4:
        return rng.choice(_SCHEMES)
    if kind == 5:
        return rng.choice(["=", ":", "&", ";", "://", ": ", "&password=", ";Password=", "@", "/", '"'])
    if kind == 6:
        return rng.choice(["[redacted]", "redacted", "[redacted]x", "p/ss w0rd", "hunter2"])
    if kind == 7:
        words = re.findall(r"\S+", text)
        return rng.choice(words) if words else "deploy"
    return _value(rng)


def corpus(size=CORPUS_SIZE, seed=SEED):
    """Deterministic (text, credential values) pairs; 0 to 3 values each."""
    rng = random.Random(seed)
    for _ in range(size):
        pieces: list[str] = []
        text = _fragment(rng, pieces)
        for _ in range(rng.choice([0, 0, 1, 2])):
            text += rng.choice([" ", "\n", "; ", ", ", "&", "", "\n\n"]) + _fragment(rng, pieces)
        values = tuple(_env_value(rng, text, pieces) for _ in range(rng.choice([0, 0, 1, 2, 3])))
        yield text, values


def _bits(spans) -> int:
    mask = 0
    for start, end in spans:
        mask |= ((1 << (end - start)) - 1) << start
    return mask


def _scrubbed_values(values):
    """The values the redactor scrubs: a value inside the placeholder is skipped."""
    return [value for value in dict.fromkeys(values) if value and value not in REDACTION_PLACEHOLDER]


def _scrub_first_bits(text, values, removed=reference.removed_spans) -> int:
    """Characters of *text* that scrubbing the values and then redacting
    removes: all values together and, with more than one, each on its own."""
    accepted = _scrubbed_values(values)
    if not accepted:
        return 0
    bits = _bits(reference.scrub_then_removed_spans(text, accepted, removed))
    if len(accepted) > 1:
        for value in accepted:
            bits |= _bits(reference.scrub_then_removed_spans(text, [value], removed))
    return bits


def _current_removed_spans(text):
    """:func:`secret_spans` in the shape of :func:`reference.removed_spans`."""
    return None, secret_spans(text)[0]


def _problems(text, values):
    """Why the redaction of (*text*, *values*) falls below a reference, if it does:
    the merge base without values ("env-free") or after scrubbing them ("with
    values"), this redactor after scrubbing them ("scrub first"), or a kept
    character of a value ("value piece")."""
    letters = _bits(m.span() for m in _LETTER_OR_DIGIT.finditer(text))
    problems = []
    plain_spans, _ = secret_spans(text)
    main_bits = _bits(reference.removed_spans(text)[1])
    if bool(main_bits & ~_bits(plain_spans) & letters):
        problems.append("env-free")
    if values:
        spans, _ = secret_spans_with_values(text, values)
        kept = ~_bits(spans) & letters
        if bool((main_bits | _scrub_first_bits(text, values)) & kept):
            problems.append("with values")
        if bool((_bits(plain_spans) | _scrub_first_bits(text, values, _current_removed_spans)) & kept):
            problems.append("scrub first")
        if _bits(reference.occurrence_spans(text, _scrubbed_values(values))) & ~_bits(spans):
            problems.append("value piece")
    return problems


def test_the_reference_spans_reproduce_the_verbatim_merge_base_output():
    for text, _ in corpus(size=5_000, seed=SEED + 1):
        expected = reference.redact_secrets(text)[0]
        output, spans = reference.removed_spans(text)
        assert output == expected, text
        assert _render(text, reference.merge_spans(spans)) == expected, text


def _without_comments_or_space(pattern: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"(?m)#.*$", "", pattern))


def test_the_floor_rules_are_the_merge_base_rules_unchanged():
    from bobi.redact import _FLOOR_KV, _FLOOR_TOKEN

    assert _FLOOR_KV.pattern == reference._SECRET_KV.pattern and _FLOOR_KV.flags == reference._SECRET_KV.flags
    assert _without_comments_or_space(_FLOOR_TOKEN.pattern) == \
        _without_comments_or_space(reference._SECRET_TOKEN.pattern)
    assert _FLOOR_TOKEN.flags == reference._SECRET_TOKEN.flags


def test_span_functions_match_the_redacted_text():
    for text, values in corpus(size=5_000, seed=SEED + 2):
        spans, _ = secret_spans(text)
        assert redact_secrets(text)[0] == _render(text, spans)
        spans, _ = secret_spans_with_values(text, values)
        assert redact_with_values(text, values)[0] == _render(text, spans)
        if "/" not in text and "\\" not in text:
            assert prepare_task(text, repo_path="/repo", max_bytes=1 << 20, secrets=values)[0] == \
                redact_with_values(text, values)[0]


def _render(text, spans):
    out, position = [], 0
    for start, end in spans:
        out += [text[position:start], REDACTION_PLACEHOLDER]
        position = end
    return "".join(out) + text[position:]


def test_redaction_never_keeps_what_the_merge_base_removes():
    started = time.monotonic()
    failures: dict[str, list] = {}
    cases = 0
    for text, values in corpus():
        cases += 1
        for problem in _problems(text, values):
            failures.setdefault(problem, []).append((text, values))
    elapsed = time.monotonic() - started
    assert cases >= min(CORPUS_SIZE, 100_000)
    summary = {problem: (len(found), found[:3]) for problem, found in failures.items()}
    assert not failures, (cases, round(elapsed, 1), summary)


@pytest.mark.parametrize("text,values", [
    ("api_key=sk-abCD12abCD12abCD12abCD12&password=hunter2", ()),
    ("Server=db;Password=AKIAABCDEFGHIJKLMNOP;ApiKey=hunter2", ()),
    ("password=AKIAABCDEFGHIJKLMNOP:fakefake1234", ()),
    ("client_secret=Zm9vYmFyYmF6Zm9vYmFyYmF6Zm9vYmFyYmF6Zm9vYmFyYmF6/fakeFAKEfake1234fakeFAKEfake12+tailTAIL99==",
     ()),
    ("postgres://p/ss w0rd:hunter2@h", ("postgres", "p/ss w0rd")),
])
def test_known_inputs_redact_at_least_as_much_as_every_reference(text, values):
    assert not _problems(text, values)


def test_a_value_that_starts_with_an_earlier_redaction_is_redacted_to_its_end():
    assert redact_secrets("api_key=sk-abCD12abCD12abCD12abCD12&password=hunter2")[0] == "api_key=[redacted]"
    assert redact_secrets("password=AKIAABCDEFGHIJKLMNOP:fakefake1234")[0] == "password=[redacted]"


def test_one_credential_value_never_hides_the_context_another_needs():
    task, _ = prepare_task("postgres://p/ss w0rd:hunter2@h", repo_path="/repo", max_bytes=8192,
                           secrets=("postgres", "p/ss w0rd"))
    assert "hunter2" not in task and "w0rd" not in task
