"""End-to-end coverage for the framework-bug RCA loop.

Drives the real ``bobi`` CLI as a subprocess against a real HTTP server that
speaks GitHub's issue/search/comment subset. Nothing is mocked: argv, click,
httpx, the socket, the JSON, and the exit codes are the ones an agent's shell
actually produces. That matters here because every requirement on this feature
is about what reaches the tracker - one issue instead of fifty, a comment
instead of a duplicate, nothing at all when the operator says off.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

REPO = "example/support"


class FakeGitHub:
    """The GitHub subset the feedback sink talks to, over a real socket."""

    def __init__(self):
        self.issues: list[dict] = []
        self.comments: list[dict] = []
        self.searches: list[str] = []
        self.search_status = 200
        self._next_number = 100

    def add_issue(self, title, state="open"):
        self._next_number += 1
        issue = {"number": self._next_number, "title": title, "state": state}
        self.issues.append(issue)
        return issue

    def search(self, query):
        self.searches.append(query)
        terms = [t for t in query.split() if ":" not in t]
        return [
            issue for issue in self.issues
            if any(term in issue["title"].casefold() for term in terms)
        ]

    def create_issue(self, payload):
        issue = self.add_issue(payload["title"])
        issue["body"] = payload.get("body", "")
        issue["labels"] = payload.get("labels", [])
        issue["created_by_request"] = True
        return issue

    @property
    def created(self):
        return [i for i in self.issues if i.get("created_by_request")]


def _handler_for(state: FakeGitHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/search/issues":
                return self._reply(404, {"message": "no such route"})
            if state.search_status != 200:
                state.searches.append(parse_qs(url.query)["q"][0])
                return self._reply(state.search_status, {"message": "rate limited"})
            hits = state.search(parse_qs(url.query)["q"][0])
            self._reply(200, {"total_count": len(hits), "items": [
                {"number": i["number"], "title": i["title"], "state": i["state"]}
                for i in hits
            ]})

        def do_POST(self):
            payload = json.loads(
                self.rfile.read(int(self.headers["Content-Length"])) or b"{}"
            )
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts[:1] != ["repos"] or parts[3:4] != ["issues"]:
                return self._reply(404, {"message": "no such route"})
            repo = f"{parts[1]}/{parts[2]}"
            if len(parts) == 6 and parts[5] == "comments":
                number = int(parts[4])
                state.comments.append({"number": number, "body": payload["body"]})
                return self._reply(201, {
                    "html_url":
                        f"https://github.com/{repo}/issues/{number}#issuecomment-1",
                })
            if len(parts) == 4:
                issue = state.create_issue(payload)
                return self._reply(201, {
                    "number": issue["number"],
                    "html_url":
                        f"https://github.com/{repo}/issues/{issue['number']}",
                })
            self._reply(404, {"message": "no such route"})

    return Handler


@pytest.fixture
def github(tmp_path):
    """A live fake GitHub plus a ``bobi`` runner pointed at it."""
    state = FakeGitHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    def run(*args, env=None, timeout=30):
        child = {
            **os.environ,
            "BOBI_HOME": str(tmp_path / "home"),
            "BOBI_GITHUB_API_URL": f"http://{host}:{port}",
            "BOBI_FEEDBACK_REPO": REPO,
            "GITHUB_TOKEN": "test-token",
        }
        child.pop("BOBI_ROOT", None)
        child.pop("BOBI_FRAMEWORK_RCA", None)
        child.pop("BOBI_FRAMEWORK_RCA_ACTIVE", None)
        child.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "bobi.cli", "feedback", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(tmp_path), env=child,
        )

    state.run = run
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


REPORT = (
    "**What broke:** `bobi agent x restart` exits 0 without restarting.\n"
    "**Trigger:** bobi agent x restart\n"
    "**Error:** manager pid file kept the dead pid\n"
    "**Suspected cause:** service.py restart, stale pid not cleared\n"
    "**Reproduces:** yes"
)


def test_rca_opens_one_issue_when_the_bug_is_new(github):
    result = github.run(
        "bug", "--rca", "--json",
        "--title", "restart exits 0 without restarting the manager",
        "--body", REPORT,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "created"
    assert len(github.created) == 1
    assert github.created[0]["title"] == (
        "restart exits 0 without restarting the manager"
    )
    assert github.comments == []
    # It looked before it wrote, scoped to the destination repo.
    assert github.searches and f"repo:{REPO}" in github.searches[0]


def test_recurring_bug_comments_on_the_existing_report(github):
    existing = github.add_issue(
        "restart exits 0 without restarting the manager",
    )

    result = github.run(
        "bug", "--rca", "--json",
        "--title", "restart exits 0 without restarting the manager",
        "--body", REPORT,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "commented"
    assert payload["number"] == existing["number"]
    assert github.created == []
    assert [c["number"] for c in github.comments] == [existing["number"]]
    assert "Seen again" in github.comments[0]["body"]
    assert len(github.comments[0]["body"]) < 1400


def test_ten_recurrences_never_become_ten_issues(github):
    """The requirement in one test: a bug that fires repeatedly stays one issue."""
    for _ in range(10):
        result = github.run(
            "bug", "--rca", "--json",
            "--title", "restart exits 0 without restarting the manager",
            "--body", REPORT,
        )
        assert result.returncode == 0, result.stderr

    assert len(github.created) == 1
    assert len(github.comments) == 9


def test_a_different_bug_in_the_same_area_still_gets_its_own_issue(github):
    """Search returns near neighbours; the match floor is what rejects them.

    Both titles are about ``restart``, so GitHub's title search hands back the
    existing issue as a candidate. Only the overlap floor stops a second,
    genuinely different bug from being buried as a comment on the first.
    """
    github.add_issue("restart exits 0 without restarting the manager")

    result = github.run(
        "bug", "--rca", "--json",
        "--title", "restart deletes the worktree of a live run",
        "--body", REPORT,
    )

    assert [q for q in github.searches if "restart" in q], "search was not exercised"
    assert json.loads(result.stdout)["action"] == "created"
    assert len(github.created) == 1
    assert github.comments == []


def test_off_switch_files_nothing_and_prints_no_procedure(github):
    off = {"BOBI_FRAMEWORK_RCA": "off"}

    guide = github.run("rca", "--error", "restart exits 0", env=off)
    filing = github.run(
        "bug", "--rca", "--json", "--title", "restart exits 0",
        "--body", REPORT, env=off,
    )

    # Nothing to follow, nothing filed, and neither call fails the caller.
    assert guide.returncode == 0 and guide.stdout.strip() == ""
    assert "disabled" in guide.stderr
    assert filing.returncode == 0
    assert json.loads(filing.stdout)["action"] == "skipped"
    assert github.issues == [] and github.comments == [] and github.searches == []


def test_the_procedure_is_printed_and_names_the_filing_command(github):
    result = github.run("rca", "--error", "restart exits 0 without restarting")

    assert result.returncode == 0, result.stderr
    assert "bobi feedback bug --rca" in result.stdout
    assert "Never run an RCA on an RCA" in result.stdout
    assert "restart exits 0 without restarting" in result.stdout


def test_an_rca_subagent_cannot_start_another_rca(github):
    """The recursion guard the sub-agent launch line sets on its own child."""
    result = github.run(
        "rca", "--error", "the RCA itself crashed",
        env={"BOBI_FRAMEWORK_RCA_ACTIVE": "1"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert "Already inside a framework bug RCA" in result.stderr


def test_a_failed_duplicate_check_files_nothing_but_never_fails_the_caller(github):
    github.search_status = 403

    result = github.run(
        "bug", "--rca", "--json", "--title", "restart exits 0",
        "--body", REPORT,
    )

    # No search means no duplicate protection, so an unattended writer stops.
    assert result.returncode == 0
    assert json.loads(result.stdout)["error"] == "dedupe_unavailable"
    assert github.created == [] and github.comments == []


def test_a_human_report_survives_a_failed_duplicate_check(github):
    github.search_status = 403

    result = github.run(
        "bug", "--json", "--title", "restart exits 0", "--body", REPORT,
    )

    # The opposite call: a person's explicit report is not lost to a rate limit.
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "created"
    assert len(github.created) == 1
    assert "Duplicate check failed" in result.stderr


def test_rca_truncates_a_wall_of_text_before_it_reaches_the_tracker(github):
    result = github.run(
        "bug", "--rca", "--json",
        "--title", "restart exits 0 " + "and keeps going " * 40,
        "--body", "chain of thought. " * 400,
    )

    assert result.returncode == 0, result.stderr
    filed = github.created[0]
    assert len(filed["title"]) <= 120
    assert filed["title"].endswith("[truncated]")
    # The body carries the allowlisted context footer on top of the clamp.
    assert len(filed["body"]) < 1200 + 400
    assert "[truncated]" in filed["body"]
