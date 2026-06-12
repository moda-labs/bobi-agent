"""Event reactor — deterministic auto-dispatch of workflows on event match.

When events arrive at the drain loop, the reactor checks each event against
a set of rules. If a rule matches, it launches the corresponding workflow
without waiting for the LLM to decide. This makes PR review feedback handling
(and other configured patterns) deterministic instead of prompt-dependent.

Rules are defined in agent.yaml under ``auto_dispatch``.
"""

from __future__ import annotations

import logging
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

    def matches(self, event: dict) -> bool:
        """Return True if the event matches this rule's type and field conditions."""
        if event.get("type") != self.event:
            return False
        if not self.match:
            return True
        fields = event.get("fields", {})
        return all(fields.get(k) == v for k, v in self.match.items())

    def dedup_key(self, event: dict) -> str:
        """Build a dedup key from the event to prevent rapid duplicate dispatches."""
        topics = event.get("topics", [])
        topic = topics[0] if topics else "unknown"
        number = event.get("fields", {}).get("number", "unknown")
        return f"{self.workflow}:{topic}:{number}"


class EventReactor:
    """Checks events against auto-dispatch rules and launches workflows."""

    def __init__(self, rules: list[AutoDispatchRule], cwd: str):
        self.rules = rules
        self.cwd = cwd
        self._dispatched: dict[str, float] = {}  # dedup_key → timestamp

    @classmethod
    def from_config(cls, config: list[dict], cwd: str) -> "EventReactor":
        """Build a reactor from the auto_dispatch config list."""
        rules = []
        for entry in config:
            rules.append(AutoDispatchRule(
                event=entry["event"],
                workflow=entry["workflow"],
                match=entry.get("match", {}),
                cooldown=entry.get("cooldown", DEFAULT_COOLDOWN),
            ))
        return cls(rules=rules, cwd=cwd)

    def process(self, event: dict) -> bool:
        """Check event against rules. If matched, dispatch and return True."""
        for rule in self.rules:
            if not rule.matches(event):
                continue

            key = rule.dedup_key(event)
            now = time.monotonic()
            if key in self._dispatched and now - self._dispatched[key] < rule.cooldown:
                log.info("Auto-dispatch skipped (cooldown): %s", key)
                return False

            self._dispatched[key] = now
            self._prune_dispatched(now)
            self._dispatch(rule, event, key)
            return True

        return False

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

        log.info("Auto-dispatching %s for %s", rule.workflow, key)
        try:
            modastack.subagent.launch_agent(
                task=task,
                cwd=self.cwd,
                workflow_name=rule.workflow,
                role="engineer",
                input_fields=input_fields,
            )
        except RuntimeError as e:
            # Session already active — the workflow is already handling this PR
            log.info("Auto-dispatch launch skipped (already active): %s — %s", key, e)
        except Exception:
            log.exception("Auto-dispatch failed for %s", key)

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
