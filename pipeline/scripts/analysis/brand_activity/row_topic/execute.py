"""CLI wrapper for the row-level topic assignment runner.

The implementation lives with the existing auto-topic modules to keep imports
stable; this package is the reproducible operator-facing entry point.
"""

from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_execute import main


if __name__ == "__main__":
    raise SystemExit(main())

