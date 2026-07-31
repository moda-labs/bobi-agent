"""Launch lineage - the ordered chain of runs that led to a launch.

A first-resort admission gate in front of the spend governor. It refuses two
shapes before any process is spawned:

1. **Self-recursion** - a run launching a named workflow already in its own
   chain. ``adhoc`` is exempt: the role prompts teach ``-w adhoc --wait``
   delegation, so nesting it is ordinary work and depth governs it instead.
2. **Depth** - a chain deeper than ``max_launch_depth``.

The depth rule is what catches the reported incident (#849). Every generation
there carried a fresh ``adhoc-<uuid4[:8]>`` run key, so the recursion was
plausibly ``adhoc`` launching ``adhoc`` and no same-workflow rule could fire.

## The chain lives in the environment, and nowhere else

``BOBI_LAUNCH_LINEAGE`` holds the JSON chain including the process's own link.
The process environment is exactly the right carrier because it is the one
thing a `bobi` CLI run from an agent's shell inherits - which is the call the
incident was made of. It is deliberately *not* persisted onto the session
registry: forensics ride the existing ``agent/session.started`` event instead.
Persisting it would mean merging over ``Session.start()``'s re-registration,
a full ``asdict`` overwrite that zeroes 17 fields (including ``emit_confirmed``,
which ``mark_terminal`` only ever sets True, so a wrong merge rule makes the
reconciler silently skip a later run's terminal event). That is the highest
regression risk in the area and it buys nothing this guard needs.

## The propagation seam

``child_agent_env()`` always **strips** the variable; only :func:`stamp`,
called at a launch site, sets it. That helper has four callers and only one is
an agent launch - if it stamped lineage, an agent running the routine
``bobi agent <x> restart`` would copy its own chain into the new manager
daemon, which then lives for days at depth N and refuses work team-wide with
no visible cause. Stripping makes a manager spawn a root for free.

## The session-name invariant

A stamped link must name the session the child actually registers. A link
naming a session that never exists makes every chain containing it unreadable,
which is the only reason to carry the chain at all. Launch sites therefore
stamp the same name they register, deriving it once rather than recomputing it
at the stamp site where it would silently drift.

## Known limits

Advisory, not a security boundary: both variables sit in the agent's own shell,
so clearing the chain - or raising the cap - resets the guard. This is a rail
against *accidental* recursion, which is what the incident was; the spend
governor stays the backstop. It also covers direct launches only - a cycle
mediated by the event bus attributes the new run to the manager process, and
that shape belongs to the event-server circuit breaker.

One more window is inherent: a process that started before this code existed
carries no chain, so its launches read as roots until it ends. The mitigation
is the precedent already in the tree - restart the manager after upgrading.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The environment variable carrying the chain, root first, own link last.
LINEAGE_ENV_VAR = "BOBI_LAUNCH_LINEAGE"

#: The framework's "no structured lifecycle" workflow, exempt from the
#: self-recursion rule. The same string ``cli.py``'s help documents.
ADHOC_WORKFLOW = "adhoc"

#: Deployment-wide override for the depth cap, read ahead of agent.yaml so an
#: operator can move the ceiling on a running fleet without rebuilding the team
#: image. Like :data:`LINEAGE_ENV_VAR`, it is deliberately never named in a
#: refusal message: that would hand the blocked agent its own bypass.
MAX_DEPTH_ENV_VAR = "BOBI_MAX_LAUNCH_DEPTH"

#: Default chain depth cap. Override in agent.yaml with `max_launch_depth`,
#: or per-deployment with `BOBI_MAX_LAUNCH_DEPTH`.
#: Grounded rather than guessed: reconstructing chains by hand from the roles
#: and phases of the live deployment's sessions, the deepest real chain is 2-3
#: (manager, then a lifecycle engineer, then an adhoc helper) and the deepest
#: the role prompts can produce is 4. 8 leaves real headroom while bounding the
#: reported incident at 8 instead of 50. A false refusal blocks honest work and
#: is hard to diagnose; three extra runs are cheap.
DEFAULT_MAX_LAUNCH_DEPTH = 8

_ROOT_LABEL = "(root)"


@dataclass(frozen=True)
class LineageLink:
    """One run in a chain.

    Keyed on ``session`` - the registry's actual key - rather than on
    ``run_key``, which is not unique: ``spawn_adhoc`` derives
    ``adhoc-<sha256(task)[:8]>`` from task text, so the same run key recurs
    across workflows. ``workflow`` is carried explicitly and never read back
    from the registry, whose ``phase`` drifts to the current step name mid-run
    and is therefore not a stable workflow identifier. It is ``""`` for a bare
    persistent session, which executes no workflow.
    """

    session: str
    workflow: str
    run_key: str


class LaunchBlockedError(RuntimeError):
    """A launch refused at admission.

    One error type, not two: a depth block is not recursion, so a dedicated
    ``RecursiveLaunchError`` would stamp a false cause on the refusal for the
    very bug this ticket reports. ``rule`` carries which one fired, using the
    same vocabulary as the event payload.
    """

    def __init__(self, message: str, *, rule: str) -> None:
        super().__init__(message)
        self.rule = rule


def encode(chain: tuple[LineageLink, ...]) -> str:
    """Serialize *chain* for the environment."""
    return json.dumps([asdict(link) for link in chain], separators=(",", ":"))


def decode(raw: str | None, *, project_path: Path | None = None) -> tuple[LineageLink, ...]:
    """Parse a chain from its environment encoding.

    Absent or empty is a root and is silent - that is every first launch, and
    every process that started before this code existed. Present but
    unparseable is tolerated as a root too, but emits
    ``system/launch.lineage.dropped``: failing open silently would reproduce
    this ticket's exact failure mode, a guard that is not there with nothing
    visible to say so.
    """
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        _emit_lineage_dropped(raw, "not valid JSON", project_path)
        return ()
    if not isinstance(parsed, list):
        _emit_lineage_dropped(raw, "not a JSON list", project_path)
        return ()

    links: list[LineageLink] = []
    for item in parsed:
        if not isinstance(item, dict):
            _emit_lineage_dropped(raw, "entry is not an object", project_path)
            return ()
        session = item.get("session")
        if not isinstance(session, str) or not session:
            _emit_lineage_dropped(raw, "entry has no session name", project_path)
            return ()
        links.append(LineageLink(
            session=session,
            workflow=str(item.get("workflow") or ""),
            run_key=str(item.get("run_key") or ""),
        ))
    return tuple(links)


def current_lineage(
    env: dict[str, str] | None = None, *, project_path: Path | None = None,
) -> tuple[LineageLink, ...]:
    """The chain this process is running under, root first."""
    source = os.environ if env is None else env
    return decode(source.get(LINEAGE_ENV_VAR), project_path=project_path)


def stamp(env: dict[str, str], chain: tuple[LineageLink, ...]) -> None:
    """Write *chain* into *env*. Called only at a launch site."""
    env[LINEAGE_ENV_VAR] = encode(chain)


def stamp_root(env: dict[str, str], link: LineageLink) -> None:
    """Stamp *link* as a chain ROOT, discarding anything inherited.

    Deliberately not :func:`admit`. A long-lived process that boots its own
    session - the manager daemon - starts a new chain; inheriting the chain of
    whatever agent happened to run ``bobi agent <x> restart`` would park it at
    depth N for its whole lifetime.
    """
    stamp(env, (link,))


def strip(env: dict[str, str]) -> None:
    """Remove any inherited chain from *env*. Stale identity is never inherited."""
    env.pop(LINEAGE_ENV_VAR, None)


def render(chain: tuple[LineageLink, ...]) -> str:
    """The chain as an operator reads it: ``a > b > c``."""
    if not chain:
        return _ROOT_LABEL
    return " > ".join(link.session for link in chain)


def max_launch_depth(root: Path) -> int:
    """The configured depth cap, or the default.

    Precedence: the :data:`MAX_DEPTH_ENV_VAR` override, then agent.yaml's
    ``max_launch_depth``, then :data:`DEFAULT_MAX_LAUNCH_DEPTH`. The env
    override comes first because it is the operator's lever on a running fleet,
    while agent.yaml is the team's declared default baked into the image.

    Both configured forms follow the existing ``0 = use default`` idiom of
    ``spend_cap`` and ``max_concurrent_agents``. An unreadable config falls
    back rather than blocking every launch on the deployment.
    """
    override = _depth_from_env()
    if override:
        return override
    try:
        from bobi.config import Config
        configured = Config.load(root).max_launch_depth
    except Exception:
        log.debug("Could not read max_launch_depth; using default", exc_info=True)
        return DEFAULT_MAX_LAUNCH_DEPTH
    return configured or DEFAULT_MAX_LAUNCH_DEPTH


def _depth_from_env() -> int:
    """The env override, or 0 when unset or unusable.

    A junk or non-positive value falls back instead of raising. This runs on
    every launch, so a typo in a fleet-wide variable must degrade to the
    configured cap, never block the deployment - but it warns, because silently
    ignoring an operator's explicit cap is how a guard ends up not guarding.
    """
    raw = os.environ.get(MAX_DEPTH_ENV_VAR, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        log.warning("Ignoring non-integer %s=%r", MAX_DEPTH_ENV_VAR, raw)
        return 0
    if value < 1:
        log.warning("Ignoring non-positive %s=%r", MAX_DEPTH_ENV_VAR, raw)
        return 0
    return value


def admit(
    root: Path, *, session: str, workflow: str, run_key: str,
) -> tuple[LineageLink, ...]:
    """Admit a launch under the launching process's chain.

    The one entry point both launch sites call, so the guard cannot be applied
    at one and forgotten at the other. Returns the child's chain; raises
    :class:`LaunchBlockedError` when either rule fires.

    ``session`` must be the name the child actually registers - the lineage
    invariant. Callers pass it rather than letting this derive one, so it
    cannot drift from the name the registration uses.
    """
    return extend(
        current_lineage(project_path=root),
        LineageLink(session=session, workflow=workflow, run_key=run_key),
        max_depth=max_launch_depth(root),
        project_path=root,
    )


def extend(
    chain: tuple[LineageLink, ...],
    link: LineageLink,
    *,
    max_depth: int,
    project_path: Path | None = None,
) -> tuple[LineageLink, ...]:
    """Admit *link* under *chain* and return the child's chain.

    Raises :class:`LaunchBlockedError` when either rule fires. Self-recursion
    is checked first: it is the more precise cause, and reporting "depth" for
    a loop would mislead exactly where diagnosis matters.
    """
    if _is_self_recursive(chain, link):
        message = _recursion_refusal(chain, link)
        _emit_blocked("recursion", chain, link, message, project_path)
        raise LaunchBlockedError(message, rule="recursion")

    child = chain + (link,)
    if len(child) > max_depth:
        message = _depth_refusal(child, max_depth)
        _emit_blocked("depth", chain, link, message, project_path)
        raise LaunchBlockedError(message, rule="depth")

    if len(child) == max_depth - 1:
        _emit_depth_approaching(child, max_depth, project_path)
    return child


def _is_self_recursive(chain: tuple[LineageLink, ...], link: LineageLink) -> bool:
    """Is *link*'s workflow already running somewhere in *chain*?

    A bare persistent session (``workflow == ""``) executes no workflow, so it
    can never collide - two managers in one chain is legitimate nesting, not a
    loop. ``adhoc`` is exempt by design, not by oversight: the exemption is the
    boundary between the two rules, with depth governing ``adhoc``.
    """
    if not link.workflow or link.workflow == ADHOC_WORKFLOW:
        return False
    return any(prior.workflow == link.workflow for prior in chain)


# The block is deterministic and the reader is an LLM. Four requirements, each
# load-bearing: name the chain and the rule so the refusal is diagnosable
# without reading logs; say it is deterministic, or an agent reads a bare
# refusal as a rate limit and waits; forbid each retry vector BY NAME, because
# "do the work inline" alone is a positive instruction and an agent that has
# not been told not to will still try once more with a fresh run key; and give
# an escalation path, or a blocked agent invents one.
#
# Explicit non-goal, asserted by a test: this text never mentions that clearing
# the environment variable resets the chain to root. The docs say so, because
# an operator needs to know the guard's limits. The text an agent reads must
# not hand it the bypass.
_DETERMINISM = (
    "This is a deterministic block, not a rate limit. Retrying will be blocked "
    "identically - do not retry with a new run key, new task text, -w adhoc, "
    "or after waiting."
)
_INLINE = (
    "Do this task yourself in the current session. If it truly cannot be done "
    "inline, stop and report this message to whoever asked for the work; "
)


def _depth_refusal(child: tuple[LineageLink, ...], max_depth: int) -> str:
    return (
        f"Launch blocked: depth. {render(child)} "
        f"(depth {len(child)}, max_launch_depth {max_depth}).\n"
        f"{_DETERMINISM}\n"
        f"{_INLINE}raising the cap is an operator edit to max_launch_depth "
        f"in agent.yaml."
    )


def _recursion_refusal(chain: tuple[LineageLink, ...], link: LineageLink) -> str:
    return (
        f"Launch blocked: self-recursion. Workflow '{link.workflow}' is "
        f"already running in this chain: {render(chain)}. Launching it again "
        f"would loop.\n"
        f"{_DETERMINISM}\n"
        f"{_INLINE}this block is not raisable by config - the chain has to "
        f"change."
    )


def _emit_blocked(
    rule: str,
    chain: tuple[LineageLink, ...],
    link: LineageLink,
    message: str,
    project_path: Path | None,
) -> None:
    """Announce a refusal on the bus.

    One event for both rules with ``rule`` naming which fired, mirroring
    :class:`LaunchBlockedError`: a topic called ``recursion.blocked`` would
    stamp a false cause on a depth block. A raised error alone lands only in
    the launching agent's transcript, and the operator seeing a *chain* rather
    than 50 unrelated launches is the third thing this ticket asks for.
    """
    _post("system/launch.blocked", {
        "rule": rule,
        "lineage": render(chain),
        "depth": len(chain) + 1,
        "session": link.session,
        "workflow": link.workflow,
        "run_key": link.run_key,
        "text": message,
    }, project_path)


def _emit_depth_approaching(
    child: tuple[LineageLink, ...], max_depth: int, project_path: Path | None,
) -> None:
    """Warn one level below the cap - the last depth at which a deeper launch
    still succeeds, so an operator has lead time before work starts failing."""
    _post("system/launch.depth.approaching", {
        "lineage": render(child),
        "depth": len(child),
        "max_launch_depth": max_depth,
        "text": (
            f"Launch chain approaching the depth cap: {render(child)} "
            f"(depth {len(child)}, max_launch_depth {max_depth}). One more "
            f"level is admitted; the next is refused."
        ),
    }, project_path)


def _emit_lineage_dropped(raw: str, reason: str, project_path: Path | None) -> None:
    """Named for what the guard did, which is the operator's actual question."""
    _post("system/launch.lineage.dropped", {
        "reason": reason,
        "raw": raw[:200],
        "text": (
            f"Launch lineage dropped: {reason}. This launch is treated as a "
            f"chain root, so the depth guard restarts from zero for it."
        ),
    }, project_path)


def _post(topic: str, payload: dict, project_path: Path | None) -> None:
    """Best-effort emit. A failed alert must never change an admission decision."""
    try:
        from bobi.events.publish import post_event
        post_event(topic, payload, project_path=project_path)
    except Exception:
        log.warning("Failed to emit %s", topic, exc_info=True)
