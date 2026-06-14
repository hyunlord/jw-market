from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from pipeline.etl.io.enrich.catalog import (
    CATALOG_OUTPUT_DIR,
    all_ml_ids,
    load_market_metadata,
    load_ml_market,
    load_strategic_product,
    ml_data_source,
)
from pipeline.etl.io.enrich.iqvia_nsa_bridge import write_iqvia_nsa_ml
from pipeline.etl.io.enrich.normalize import load_customer_dictionary
from pipeline.etl.io.enrich.schema import EnrichResult
from pipeline.etl.io.enrich.ubist_bridge import write_empty_ml, write_ubist_ml
from pipeline.etl.lib.ops_utils import find_project_root, first_existing


REPO_ROOT = find_project_root(Path(__file__).resolve())
UBIST_DIR = first_existing(REPO_ROOT / "output" / "ubist", REPO_ROOT / "parquet" / "ubist")
IQVIA_NSA_DIR = first_existing(REPO_ROOT / "output" / "iqvia_nsa", REPO_ROOT / "parquet" / "iqvia_nsa")
ENRICHED_DIR = REPO_ROOT / "output" / "enriched"
AUDIT_DIR = REPO_ROOT / "audit" / "phase16d_layer2"


def enrich_ml(
    ml_id: str,
    *,
    audit_dir: Path = AUDIT_DIR,
    output_dir: Path = ENRICHED_DIR,
    catalog_root: Path = CATALOG_OUTPUT_DIR,
    ubist_dir: Path = UBIST_DIR,
    iqvia_nsa_dir: Path = IQVIA_NSA_DIR,
    ingested_at: str | None = None,
    source_filter: str = "all",
) -> EnrichResult:
    audit_dir.mkdir(parents=True, exist_ok=True)
    customer_dict = load_customer_dictionary()
    metadata = load_market_metadata()
    ml_market = load_ml_market(catalog_root)
    ml_rows = ml_market[ml_market["ml_id"] == ml_id]
    if ml_rows.empty:
        raise ValueError(f"Unknown ml_id: {ml_id}")
    ml_row = ml_rows.iloc[0]
    data_source = ml_data_source(ml_row)
    products = load_strategic_product(ml_id, catalog_root)
    output_path = output_dir / f"ml_id={ml_id}" / "data.parquet"
    sources: dict[str, int] = {}
    skipped_sources: list[str] = []
    matched_products = 0
    total_rows = 0

    if output_path.exists() and source_filter == "all":
        output_path.unlink()

    run_ubist = source_filter in {"ubist", "all"} and data_source in {"ubist", "both"}
    run_nsa = source_filter in {"nsa", "iqvia", "all"} and data_source in {"iqvia", "both"}

    if run_ubist:
        if not ubist_dir.exists():
            raise FileNotFoundError(f"Missing UBIST parquet directory: {ubist_dir}")
        ubist_glob = str(ubist_dir / "year=*" / "month=*" / "data.parquet")
        rows, prod_count = write_ubist_ml(
            products,
            customer_dict,
            output_path,
            ubist_glob=ubist_glob,
            ingested_at=ingested_at,
        )
        sources["ubist"] = rows
        total_rows += rows
        matched_products = max(matched_products, prod_count)
    elif source_filter == "all":
        write_empty_ml(output_path)

    if run_nsa:
        if not iqvia_nsa_dir.exists():
            raise FileNotFoundError(f"Missing IQVIA NSA canonical parquet directory: {iqvia_nsa_dir}")
        nsa_glob = str(iqvia_nsa_dir / "*.parquet")
        nsa_stats = write_iqvia_nsa_ml(
            products,
            metadata,
            ml_id,
            ml_row,
            output_path,
            nsa_glob=nsa_glob,
            ingested_at=ingested_at,
        )
        sources["nsa"] = nsa_stats.rows
        total_rows += nsa_stats.rows
        matched_products = max(matched_products, nsa_stats.matched_products)

    if data_source in {"iqvia", "both"}:
        skipped_sources.extend(["chso:removed", "csd:removed"])

    return EnrichResult(ml_id, total_rows, matched_products, len(products), sources, skipped_sources)


def write_loading_csv(results: list[EnrichResult], audit_dir: Path) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / "enriched_summary.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "ml_id",
                "rows",
                "matched_products",
                "total_products",
                "product_match_rate",
                "sources",
                "skipped_sources",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "ml_id": result.ml_id,
                    "rows": result.rows,
                    "matched_products": result.matched_products,
                    "total_products": result.total_products,
                    "product_match_rate": f"{result.product_match_rate:.6f}",
                    "sources": json.dumps(result.sources, ensure_ascii=False),
                    "skipped_sources": ";".join(result.skipped_sources),
                }
            )


def run_layer2_ubist(
    *,
    ml_id: str | None = None,
    output_dir: Path = ENRICHED_DIR,
    audit_dir: Path = AUDIT_DIR,
    catalog_root: Path = CATALOG_OUTPUT_DIR,
    ubist_dir: Path = UBIST_DIR,
    iqvia_nsa_dir: Path = IQVIA_NSA_DIR,
    ingested_at: str | None = None,
    source_filter: str = "all",
    truncate: bool = False,
) -> list[EnrichResult]:
    if truncate and output_dir.exists():
        shutil.rmtree(output_dir)
    targets = [ml_id] if ml_id else all_ml_ids(catalog_root)
    results = [
        enrich_ml(
            target,
            audit_dir=audit_dir,
            output_dir=output_dir,
            catalog_root=catalog_root,
            ubist_dir=ubist_dir,
            iqvia_nsa_dir=iqvia_nsa_dir,
            ingested_at=ingested_at,
            source_filter=source_filter,
        )
        for target in targets
    ]
    write_loading_csv(results, audit_dir)
    return results
