"""Compatibility re-export of the deployment-identity resolver.

The resolver moved to :mod:`bobi.identity` so a leaf command can read it
without paying for ``bobi.supervisor``'s eager ``.config`` / ``.supervision``
imports (~20ms for 30 lines of ``os.environ.get``). The sidecar's own imports
and its documented dependency discipline are unchanged: this module still
exposes the same names, and ``bobi.identity`` still imports nothing but the
standard library plus a lazy ``bobi.paths``.
"""

from __future__ import annotations

from bobi.identity import (
    _detect_platform,
    _first_env,
    _resolve_instance,
    resolve_deployment_identity,
)

__all__ = [
    "resolve_deployment_identity",
    "_first_env",
    "_detect_platform",
    "_resolve_instance",
]
