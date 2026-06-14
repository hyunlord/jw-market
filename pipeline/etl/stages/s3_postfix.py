from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.etl.io.enrich.layer2_ubist import run_layer2_ubist

STAGE = "s3 enrich"


def _path_param(params: dict[str, Any], key: str) -> Path | None:
    value = params.get(key)
    return Path(str(value)) if value else None


def _str_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    return str(value) if value else None


def run(params: dict[str, Any]) -> int:
    source = str(params.get("source") or "ubist")
    if source not in {"ubist", "all"}:
        print(f"[{STAGE}] source={source}는 s3-b 이후 지원 예정입니다. 이번 s3-a는 UBIST만 실행합니다.")
        return 2
    output_dir = _path_param(params, "target_dir") or Path("output") / "enriched"
    audit_dir = _path_param(params, "audit_dir") or Path("audit") / "phase16d_layer2"
    catalog_root = _path_param(params, "catalog_root") or Path("parquet")
    ubist_dir = _path_param(params, "ubist_dir") or Path("output") / "ubist"
    ml_id = _str_param(params, "ml_id")
    ingested_at = _str_param(params, "ingested_at")
    try:
        results = run_layer2_ubist(
            ml_id=ml_id,
            output_dir=output_dir,
            audit_dir=audit_dir,
            catalog_root=catalog_root,
            ubist_dir=ubist_dir,
            ingested_at=ingested_at,
            truncate=bool(params.get("truncate")),
        )
    except Exception as exc:
        print(f"[{STAGE}] UBIST enrich 실패: {exc}")
        return 1

    for result in results:
        print(
            f"[{STAGE}] {result.ml_id}: rows={result.rows} "
            f"matched_products={result.matched_products}/{result.total_products} "
            f"sources={result.sources} skipped={result.skipped_sources}"
        )
    return 0
