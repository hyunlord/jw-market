#!/usr/bin/env python3
"""Compatibility wrapper for the old Phase zeta ai_analysis reload command.

Phase 30.3 moved Phase zeta narratives out of cache_deep_analysis.response_json
and into cache_deep_analysis_ai_analysis. This wrapper deliberately delegates to
the new dedicated-table migrator so the old command can no longer patch the
rebuilt cache payload.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    args = [
        sys.executable,
        str(root / "pipeline" / "scripts" / "etl" / "migrate_ai_analysis_table.py"),
        "--apply",
        *sys.argv[1:],
    ]
    return subprocess.call(args, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
