"""The sdist's event-server payload is chosen, not incidental.

`[tool.hatch.build.targets.sdist]` selects `event-server` as a whole directory,
so every workspace under it is in scope by default. The Cloudflare Worker
workspace is deliberately out: nothing in the Python distribution builds or
runs it, and its documented deploy path is a git clone plus `wrangler deploy`.

Without the explicit `exclude` entry the Worker still fell out of the sdist,
but only by accident - Hatch's `safe_walk` follows the npm workspace symlink
`event-server/node_modules/bobi-events-worker -> ../worker` and then prunes the
real directory as an already-visited inode, so the sdist's contents depended on
whether `npm ci` had run. These assertions read the pattern config rather than
the filesystem walk, so they hold either way: that independence IS the property
under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hatchling.builders.sdist import SdistBuilder

REPO_ROOT = Path(__file__).resolve().parent.parent

WORKER_PATHS = (
    "event-server/worker/src/index.ts",
    "event-server/worker/src/fleet.ts",
    "event-server/worker/wrangler.jsonc",
    "event-server/worker/worker-configuration.d.ts",
    "event-server/worker/test/index.spec.ts",
)

# The two workspaces the wheel's event-server artifact is actually built from
# (`bobi/events/artifact.py`). An over-broad exclusion that swallowed these
# would produce an sdist that cannot build a wheel.
SHIPPED_PATHS = (
    "event-server/src/local.ts",
    "event-server/core/src/core.ts",
    "event-server/package.json",
    "event-server/package-lock.json",
)


@pytest.fixture(scope="module")
def sdist_config():
    return SdistBuilder(str(REPO_ROOT)).config


@pytest.mark.parametrize("relative_path", WORKER_PATHS)
def test_worker_workspace_is_excluded_from_the_sdist(sdist_config, relative_path):
    # Selected by `include`, then removed by `exclude` - drop the exclude entry
    # and this flips, which is what makes the assertion non-vacuous.
    assert sdist_config.include_spec.match_file(relative_path), (
        f"{relative_path} is expected to be in scope of the sdist include list"
    )
    assert sdist_config.path_is_excluded(relative_path)


@pytest.mark.parametrize("relative_path", SHIPPED_PATHS)
def test_the_wheels_event_server_inputs_stay_in_the_sdist(sdist_config, relative_path):
    assert not sdist_config.path_is_excluded(relative_path)
    assert sdist_config.include_path(relative_path)


def test_the_worker_sources_exist_to_be_excluded():
    # Guards the tests above against passing because the paths were renamed
    # away rather than because the exclusion works.
    for relative_path in WORKER_PATHS:
        assert (REPO_ROOT / relative_path).is_file(), relative_path
