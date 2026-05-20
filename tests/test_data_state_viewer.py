from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from pipeline.scripts.viewers.build_data_state_viewer import build_html
from pipeline.scripts.viewers.collect_data_state import collect_catalog, find_enriched_jw_rows, json_safe


def test_json_safe_converts_non_json_values_and_truncates_long_strings() -> None:
    converted = json_safe(
        {
            "created_at": datetime(2026, 5, 21, 9, 30),
            "amount": Decimal("123.45"),
            "payload": "x" * 5_010,
            "raw": b"\xeb\xa6\xac\xeb\xb0\x94\xeb\xa1\x9c",
        }
    )

    assert converted["created_at"] == "2026-05-21T09:30:00"
    assert converted["amount"] == 123.45
    assert converted["raw"] == "리바로"
    assert converted["payload"].startswith("x" * 5_000)
    assert converted["payload"].endswith("... (+10 chars truncated)")


def test_collect_catalog_reports_schema_sample_and_jw_deep_sample(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "output" / "catalog" / "strategic_brand"
    catalog_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {"brand_id": "sb_001", "name": "리바로", "score": 1.5, "optional": None},
            {"brand_id": "sb_002", "name": "타사브랜드", "score": 2.5, "optional": "filled"},
        ]
    ).to_parquet(catalog_dir / "strategic_brand.parquet", index=False)

    result = collect_catalog("strategic_brand", project_root=tmp_path)

    assert result["layer"] == "catalog"
    assert result["purpose"] == "catalog"
    assert result["total_rows"] == 2
    assert result["total_columns"] == 4
    assert [row["name"] for row in result["jw_deep_sample"]] == ["리바로"]
    optional = next(col for col in result["schema"] if col["name"] == "optional")
    assert optional["null_rate"] == 50.0
    assert optional["unique_count"] == 1


def test_find_enriched_jw_rows_annotates_product_catalog_names(tmp_path: Path) -> None:
    enriched_dir = tmp_path / "output" / "enriched" / "ml_id=ml_006"
    enriched_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {"ml_id": "ml_006", "product_id": "sp_006_00771_001", "canonical_value": 100.0},
            {"ml_id": "ml_006", "product_id": "sp_other", "canonical_value": 200.0},
        ]
    ).to_parquet(enriched_dir / "data.parquet", index=False)

    rows = find_enriched_jw_rows(
        [enriched_dir / "data.parquet"],
        {
            "sp_006_00771_001": {
                "jw_product_name": "리바로 정 1mg",
                "jw_brand_id": "sb_006_00771",
                "jw_ml_id": "ml_006",
                "jw_cd_id": "cd_006",
            }
        },
        limit=10,
    )

    assert rows == [
        {
            "jw_product_name": "리바로 정 1mg",
            "jw_brand_id": "sb_006_00771",
            "jw_ml_id": "ml_006",
            "jw_cd_id": "cd_006",
            "ml_id": "ml_006",
            "product_id": "sp_006_00771_001",
            "canonical_value": 100.0,
        }
    ]


def test_build_html_creates_self_contained_viewer_with_escaped_rendering(tmp_path: Path) -> None:
    state = {
        "generated_at": "2026-05-21T09:30:00",
        "repo_commit": "8de284e1234",
        "repo_tag": "prototype-43-six-mart-full-load",
        "total_rows": 1,
        "tables": {
            "mart_general_brand_metric": {
                "layer": "layer_3_mart",
                "purpose": "mart",
                "total_rows": 1,
                "total_columns": 2,
                "schema": [
                    {
                        "name": "brand_name",
                        "type": "varchar",
                        "nullable": True,
                        "null_rate": 0.0,
                        "unique_count": 1,
                        "sample_values": ["리바로"],
                    }
                ],
                "sample_rows": [{"brand_name": "리바로", "payload": "<script>alert(1)</script>"}],
                "jw_deep_sample": [{"brand_name": "리바로"}],
                "distribution": {"source_measure": [{"source": "ubist", "measure": "sales", "count": 1}]},
                "storage_info": {"size_mb": 0.1},
            }
        },
    }
    output_path = tmp_path / "viewer.html"

    build_html(state, output_path)

    html = output_path.read_text(encoding="utf-8")
    assert "const DATA =" in html
    assert "mart_general_brand_metric" in html
    assert "function escapeHtml" in html
    assert "src=\"http" not in html
    assert "href=\"http" not in html
    assert "<script>alert(1)</script>" not in html
