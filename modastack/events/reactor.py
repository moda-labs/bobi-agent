"""Event reactor — deterministic auto-dispatch of workflows on event match.

When events arrive at the drain loop, the reactor checks each event against
a set of rules. If a rule matches, it launches the corresponding workflow
without waiting for the LLM to decide. This makes PR review feedback handling
(and other configured patterns) deterministic instead of prompt-dependent.

Rules are defined in agent.yaml under ``auto_dispatch``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import modastack.subagent  # noqa: E402 — top-level so @patch can intercept

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 1800  # 30 minutes
_MAX_DEDUP_ENTRIES = 500


@dataclass
class AutoDispatchRule:
    """A rule that matches an event type + optional field conditions to a workflow."""

    event: str
    workflow: str
    match: dict[str, str | int | bool] = field(default_factory=dict)
    cooldown: int = DEFAULT_COOLDOWN
    suppress: bool = False
    # Dispatch-hygiene guards (issue #411).
    # Skipping the bot's OWN events is the DEFAULT — a bot auto-reacting to its
    # own action is never the intent (per review, underminedsk 2026-06-22). The
    # rare rule that deliberately reacts to the bot's own action (e.g. pr-closed
    # worktree cleanup on a bot-merged PR) opts back in with allow_self_authored.
    allow_self_authored: bool = False  # opt-in: DO dispatch on the bot's own events

    def matches(self, event: dict) -> bool:
        """Return True if the event matches this rule's type and field conditions."""
        if event.get("type") != self.event:
            return False
        if not self.match:
            return True
        fields = event.get("fields", {})
        return all(fields.get(k) == v for k, v in self.match.items())

    def dedup_key(self, event: dict) -> str:
        """Build a dedup key from the event to prevent rapid duplicate dispatches.

        The key must be unique per *distinct* trigger but stable across genuine
        redelivery of the *same* trigger. A PR-level key (workflow:topic:number)
        is too coarse: every comment on a PR collapses onto one key, so the
        cooldown treats a reviewer's follow-up comments as duplicates of the
        first and silently drops them (issue #326).

        Prefer a STABLE per-comment / per-review identifier (``comment_id`` /
        ``review_id``). It is distinct per comment — so genuinely new comments
        each dispatch (#326) — yet identical across *every* delivery of that one
        comment, so a comment that reaches the reactor more than once (a webhook
        plus a monitor re-poll, each stamped with a different per-delivery id)
        collapses onto a single key instead of fanning out into multiple engines
        (issue #411). Fall back to the per-delivery event id (distinct per
        delivery, stable across stream replay), then to the PR-level key.
        """
        topics = event.get("topics", [])
        topic = topics[0] if topics else "unknown"
        fields = event.get("fields", {})
        number = fields.get("number", "unknown")
        base = f"{self.workflow}:{topic}:{number}"
        comment_id = fields.get("comment_id")
        if comment_id is not None:
            return f"{base}:comment:{comment_id}"
        review_id = fields.get("review_id")
        if review_id is not None:
            return f"{base}:review:{review_id}"
        event_id = event.get("id")
        return f"{base}:{event_id}" if event_id else base

    def run_key(self, event: dict) -> str | None:
        """Deterministic launch key anchored on the stable comment/review id.

        The dedup dict (``dedup_key``) only guards a single reactor process; the
        observed #411 fan-out (#416/#417/#418) came from *concurrent* sessions,
        each with an empty dict. Passing this key to ``launch_agent`` makes the
        resulting ``session_name`` identical for two dispatches of the same
        comment, so the persisted "A run is already active" guard rejects the
        duplicate even across processes — a second, process-independent line of
        defense behind the in-memory dedup. Returns ``None`` (→ launch_agent
        mints its own random key) when no stable id is available, preserving the
        prior behavior for events without a comment/review.
        """
        fields = event.get("fields", {})
        number = fields.get("number")
        if number is None:
            return None
        comment_id = fields.get("comment_id")
        if comment_id is not None:
            return f"{number}-comment-{comment_id}"
        review_id = fields.get("review_id")
        if review_id is not None:
            return f"{number}-review-{review_id}"
        return None

    def skip_reason(self, event: dict, self_login: str | None) -> str | None:
        """Return a reason to skip dispatch for this matched event, or None.

        Guards the spurious pr-feedback dispatch that issue #411 is really about:
        the bot reacting to its OWN comments (the self-cascade that loops the
        feedback engine onto a comment it just posted). It fails *open* — when
        the bot identity can't be resolved the event dispatches normally rather
        than being silently dropped.

        Draft PRs are deliberately NOT skipped: a held draft is exactly where we
        want feedback discussion to happen, and the self-author skip already
        stops the only loop that matters (per review, underminedsk 2026-06-22).
        """
        fields = event.get("fields", {})
        if not self.allow_self_authored and self_login and fields.get("sender") == self_login:
            return "self-authored"
        return None


class EventReactor:
    """Checks events against auto-dispatch rules and launches workflows."""

    def __init__(self, rules: list[AutoDispatchRule], cwd: str,
                 self_login: str | None = None):
        self.rules = rules
        self.cwd = cwd
        # The bot's own GitHub login, used to skip auto-dispatch on the bot's
        # own comments (issue #411). None when it could not be resolved — the
        # self-author guard then stays inactive (fail open).
        self.self_login = self_login
        self._dispatched: dict[str, float] = {}  # dedup_key → timestamp

    @classmethod
    def from_config(cls, config: list[dict], cwd: str,
                    self_login: str | None = None) -> "EventReactor":
        """Build a reactor from the auto_dispatch config list."""
        rules = []
        for entry in config:
            rules.append(AutoDispatchRule(
                event=entry["event"],
                workflow=entry.get("workflow", ""),
                match=entry.get("match", {}),
                cooldown=entry.get("cooldown", DEFAULT_COOLDOWN),
                suppress=entry.get("suppress", False),
                allow_self_authored=entry.get("allow_self_authored", False),
            ))
        return cls(rules=rules, cwd=cwd, self_login=self_login)

    def process(self, event: dict) -> str | None:
        """Check event against rules.

        Returns:
            ``"dispatched"`` if a workflow was launched,
            ``"suppressed"`` if the event matched a suppress rule (no
            workflow launched, but the event should be annotated as
            handled so the LLM doesn't act on it),
            or ``None`` if no rule matched.
        """
        for rule in self.rules:
            if not rule.matches(event):
                continue

            # Dispatch-hygiene guard (issue #411): never spin up a feedback
            # engine on the bot's own comment (the self-cascade loop).
            skip = rule.skip_reason(event, self.self_login)
            if skip:
                log.info("Auto-dispatch skipped (%s): %s",
                         skip, rule.dedup_key(event))
                return None

            key = rule.dedup_key(event)
            now = time.monotonic()
            if key in self._dispatched and now - self._dispatched[key] < rule.cooldown:
                log.info("Auto-dispatch skipped (cooldown): %s", key)
                return None

            self._dispatched[key] = now
            self._prune_dispatched(now)

            if rule.suppress:
                log.info("Auto-dispatch suppressed (no workflow): %s", key)
                return "suppressed"

            self._dispatch(rule, event, key)
            return "dispatched"

        return None

    def _prune_dispatched(self, now: float) -> None:
        """Remove expired entries so the dedup dict doesn't grow unbounded."""
        if len(self._dispatched) <= _MAX_DEDUP_ENTRIES:
            return
        max_cooldown = max((r.cooldown for r in self.rules), default=DEFAULT_COOLDOWN)
        expired = [k for k, ts in self._dispatched.items() if now - ts > max_cooldown]
        for k in expired:
            del self._dispatched[k]

    def _dispatch(self, rule: AutoDispatchRule, event: dict, key: str) -> None:
        """Launch the workflow for a matched event.

        Builds a task description from the event fields, adapting the text
        to the event type (PR review feedback, PR closed, issue assigned, etc.).
        """
        fields = event.get("fields", {})
        number = fields.get("number", "?")
        topics = event.get("topics", [])
        repo = topics[0].removeprefix("github:") if topics else "unknown"
        event_type = event.get("type", "")

        task = self._build_task(rule, event_type, fields, number, repo)

        # Pass event fields into the workflow's input scope so native
        # actions and route conditions can resolve ${{ input.* }} variables.
        input_fields = {
            "event_type": event_type,
            "repo": repo,
            "pr_number": number,
        }
        input_fields.update(fields)

        # Deterministic per-comment launch key (issue #411) — see run_key().
        run_key = rule.run_key(event)

        log.info("Auto-dispatching %s for %s", rule.workflow, key)

        # launch_agent runs the concurrency-semaphore preflight, which BLOCKS
        # (up to ~120s) when the agent cap is reached. _dispatch is called on
        # the single drain thread, so blocking here stalls the whole event
        # pipeline — no inbox delivery, no reply routing — until a slot frees.
        # Run the launch off-thread so the drain loop returns immediately; the
        # semaphore still bounds how many agents actually run at once.
        def _launch() -> None:
            try:
                modastack.subagent.launch_agent(
                    task=task,
                    cwd=self.cwd,
                    workflow_name=rule.workflow,
                    role="engineer",
                    run_key=run_key,
                    input_fields=input_fields,
                )
            except RuntimeError as e:
                # Session already active / cap timeout — the workflow is either
                # already handling this PR or the slot never opened.
                log.info("Auto-dispatch launch skipped: %s — %s", key, e)
            except Exception:
                log.exception("Auto-dispatch failed for %s", key)

        threading.Thread(
            target=_launch, name=f"dispatch-{key}", daemon=True).start()

    @staticmethod
    def _build_task(rule: AutoDispatchRule, event_type: str, fields: dict,
                    number, repo: str) -> str:
        """Build a human-readable task description from event context."""
        action = fields.get("action", "")

        # PR closed (merged or abandoned)
        if event_type == "github.pull_request" and action == "closed":
            merged = fields.get("merged", False)
            head_branch = fields.get("head_branch", "")
            parts = [f"PR #{number} in {repo} closed (merged={merged})."]
            if head_branch:
                parts.append(f"Head branch: {head_branch}.")
            parts.append("Run cleanup.")
            return " ".join(parts)

        # PR review feedback
        if "review" in event_type or "comment" in event_type:
            review_state = fields.get("review_state", "")
            parts = [f"PR #{number} in {repo} received review feedback"]
            if review_state:
                parts.append(f"(review: {review_state})")
            parts.append(f"[event: {event_type}].")
            parts.append("Address the reviewer's comments.")
            return " ".join(parts)

        # Issue assigned
        if event_type == "github.issues.assigned":
            title = fields.get("title", "")
            return f"Issue #{number} in {repo} assigned: {title}. Begin work."

        # Generic fallback
        return f"Event {event_type} on #{number} in {repo} [action: {action}]. Process via {rule.workflow}."
