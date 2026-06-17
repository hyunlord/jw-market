"""Stage 7: build additive bridge indexes for dynamic market APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.etl.io.mart.molecule_bridge import build_molecule_bridge
from pipeline.etl.lib.ops_utils import find_project_root, first_existing


STAGE = "s7 bridge"
PROJECT_ROOT = find_project_root(Path(__file__).resolve())


def _default_catalog_root() -> Path:
    """Return the same catalog root used by mart stages unless overridden."""

    return first_existing(PROJECT_ROOT / "output" / "catalog", PROJECT_ROOT / "parquet")


def run(params: dict[str, Any]) -> int:
    """Build ``mart_brand_molecule`` in an isolated target schema."""

    target_db = str(params.get("target_db") or "").strip()
    source_db = str(params.get("source_db") or "jw_mart").strip()
    if not target_db:
        print(f"[{STAGE}] 실패: --target-db is required for bridge builds")
        return 2
    catalog_root = Path(str(params.get("catalog_root") or _default_catalog_root()))
    max_rows = params.get("max_rows")
    try:
        stats = build_molecule_bridge(
            source_db=source_db,
            target_db=target_db,
            catalog_root=catalog_root,
            max_rows=int(max_rows) if max_rows else None,
        )
    except Exception as exc:
        print(f"[{STAGE}] 실패: {exc}")
        return 1
    print(
        f"[{STAGE}] 완료 target_db={stats.target_db} source_db={stats.source_db} "
        f"rows={stats.inserted_rows} candidates={stats.candidate_rows} "
        f"brand_keys={stats.brand_keys} molecules={stats.molecule_norms} combo_rows={stats.combo_rows}"
    )
    return 0
