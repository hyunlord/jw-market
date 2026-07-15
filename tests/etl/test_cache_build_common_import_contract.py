from __future__ import annotations

import ast
from pathlib import Path

from pipeline.scripts.etl import cache_build_common


def test_cache_build_common_keeps_pandas_local_to_catalog_loader() -> None:
    """API imports cache_build_common, so pandas must stay out of module import."""
    tree = ast.parse(Path("pipeline/scripts/etl/cache_build_common.py").read_text())
    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]

    assert all(
        not (
            isinstance(node, ast.Import)
            and any(alias.name == "pandas" for alias in node.names)
        )
        for node in top_level_imports
    )


def test_period_ordinal_reuses_repeated_period_parsing() -> None:
    cache_build_common._period_ordinal.cache_clear()

    assert cache_build_common._period_ordinal("2026-05") == (24316, 12)
    assert cache_build_common._period_ordinal("2026-05") == (24316, 12)
    assert cache_build_common._period_ordinal.cache_info().hits == 1
