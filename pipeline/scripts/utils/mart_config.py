"""Single source of truth for the mart database generation name.

The mart generation name doubles as the pipeline source epoch: switching to a
new mart generation means changing ``DEFAULT_MART_DB_NAME`` here and then
letting the drift gate (``tests/deploy/test_mart_db_single_source.py``)
enumerate every remaining pinned copy that must follow.

Two kinds of copies are deliberate and stay pinned:

* Kubernetes manifests keep explicit env values and ``test "$VAR" = "..."``
  guards as a fail-closed double-entry check; they must never silently follow
  a code-side rename.
* Standalone scripts (``pipeline/scripts/crawler``, ``pipeline/scripts/
  ai_analysis``) run outside the package context in the crawl image layout,
  so they keep a local default instead of importing this module.

The drift gate keeps every such copy equal to this constant, so the
generation switch procedure is: edit this file, run the gate, update every
location it reports, in one commit.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULT_MART_DB_NAME: Final[str] = "jw_mart_d2_stage_20260630_r2"

# The mart source epoch is the generation name itself.
DEFAULT_SOURCE_EPOCH: Final[str] = DEFAULT_MART_DB_NAME


def resolve_mart_db_name(*env_keys: str) -> str:
    """Return the first non-empty env value among ``env_keys``, else the default."""
    for key in env_keys:
        value = os.environ.get(key)
        if value:
            return value
    return DEFAULT_MART_DB_NAME
