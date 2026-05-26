from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from pipeline.scripts.viewers import build_data_state_viewer as viewer_builder
from pipeline.scripts.viewers import collect_data_state as data_state_collector
from pipeline.scripts.viewers.build_data_state_viewer import build_html
from pipeline.scripts.viewers.collect_data_state import (
    collect_cache_brands,
    collect_cache_cause,
    collect_cache_deep_analysis,
    collect_cache_market_status,
    collect_catalog,
    find_enriched_jw_rows,
    json_safe,
)


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


def test_build_html_embeds_data_dictionary_tab_and_schema_types(tmp_path: Path, monkeypatch) -> None:
    dictionary_path = tmp_path / "data_dictionary.json"
    dictionary_path.write_text(
        """
        {
          "_meta": {"version": "test"},
          "mart_general_brand_metric": {
            "purpose": "브랜드 단위 mart 설명",
            "row_grain": "brand × source × measure",
            "row_count_approx": "1 row",
            "etl_source": "Layer 3 general view",
            "columns": {
              "brand_name": "브랜드명 설명",
              "payload": "payload JSON 설명"
            },
            "sample_interpretation": {
              "row_example": "brand_name=리바로",
              "meaning": "리바로 브랜드의 지표 row"
            },
            "notes": ["metric_history는 JSON 문자열이다."]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(viewer_builder, "DICTIONARY_PATH", dictionary_path, raising=False)
    state = {
        "generated_at": "2026-05-21T10:00:00",
        "repo_commit": "49a535c",
        "repo_tag": "",
        "total_rows": 1,
        "tables": {
            "mart_general_brand_metric": {
                "layer": "layer_3_mart",
                "purpose": "mart",
                "total_rows": 1,
                "total_columns": 2,
                "schema": [
                    {"name": "brand_name", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "payload", "type": "longtext", "null_rate": 0.0, "unique_count": 1},
                ],
                "sample_rows": [{"brand_name": "리바로", "payload": "{}"}],
                "jw_deep_sample": [],
                "distribution": {},
                "storage_info": {},
            }
        },
    }

    output_path = tmp_path / "viewer.html"
    viewer_builder.build_html(state, output_path)

    html = output_path.read_text(encoding="utf-8")
    assert "Data Dictionary" in html
    assert "const DICTIONARY =" in html
    assert "renderTableDictionary" in html
    assert "switchToLayerTabAndSelect" in html
    assert "<details open><summary>Sample Rows</summary>" in html
    assert "브랜드 단위 mart 설명" in html
    assert "브랜드명 설명" in html
    assert "longtext" in html


def test_build_html_adds_json_modal_and_clickable_json_cells(tmp_path: Path) -> None:
    state = {
        "generated_at": "2026-05-21T11:00:00",
        "repo_commit": "d212458",
        "repo_tag": "",
        "total_rows": 1,
        "tables": {
            "mart_strategic_ml_brand_metric": {
                "layer": "layer_3_mart",
                "purpose": "mart",
                "total_rows": 1,
                "total_columns": 3,
                "schema": [
                    {"name": "brand_name", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "metric_history", "type": "longtext", "null_rate": 0.0, "unique_count": 1},
                    {"name": "payload", "type": "json", "null_rate": 0.0, "unique_count": 1},
                ],
                "sample_rows": [
                    {
                        "brand_name": "리바로",
                        "metric_history": '{"2026-Q1":{"raw_value":1234,"ms":5.2,"mom":null}}',
                        "payload": {"is_jw": True, "channels": ["KHPA", "KCPA"]},
                    }
                ],
                "jw_deep_sample": [],
                "distribution": {},
                "storage_info": {},
            }
        },
    }
    output_path = tmp_path / "viewer.html"

    build_html(state, output_path)

    html = output_path.read_text(encoding="utf-8")
    assert 'id="jsonModal"' in html
    assert "function openJsonModal" in html
    assert "function renderJsonValue" in html
    assert "function repairTruncatedJsonPrefix" in html
    assert "function applySearchHighlight" in html
    assert "function copyJsonContent" in html
    assert "function showCopyFallback" in html
    assert "function setSampleWidth" in html
    assert "json-copy-fallback" in html
    assert "json-cell-clickable" in html
    assert "json-cell-trigger" in html
    assert "Open JSON" in html
    assert "JSON_CELL_PAYLOADS" in html
    assert "sample-toggle-bar" in html
    assert "Wide JSON" in html
    assert "Wide All" in html
    assert "data-json-path" in html
    assert '" › " + col' in html


def test_load_dictionary_returns_empty_dict_when_file_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(viewer_builder, "DICTIONARY_PATH", tmp_path / "missing.json", raising=False)

    assert viewer_builder.load_dictionary() == {}


class _FakeSplitCacheCursor:
    def __init__(self) -> None:
        self.result: list[dict] = []
        self.one: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params=None) -> None:
        normalized = " ".join(query.split()).lower()
        if "count(*) as cnt" in normalized:
            if "cache_brands" in normalized:
                self.one = {"cnt": 6}
            elif "cache_market_status" in normalized:
                self.one = {"cnt": 3038}
            elif "cache_cause" in normalized:
                self.one = {"cnt": 137978}
            elif "cache_deep_analysis" in normalized:
                self.one = {"cnt": 137978}
            else:
                self.one = {"cnt": 0}
            return
        if "group by `view_type`, `source`, `measure`" in normalized:
            self.result = [
                {
                    "view_type": "strategic_ml",
                    "source": "ubist",
                    "measure": "sales",
                    "row_count": 2,
                    "rows": 2,
                    "size_mb": 1.5,
                    "avg_kb": 768.0,
                }
            ]
            return
        if "group by `view_type`, `source`" in normalized:
            self.result = [
                {
                    "view_type": "strategic_ml",
                    "source": "ubist",
                    "row_count": 1,
                    "rows": 1,
                    "size_mb": 0.03,
                    "avg_kb": 30.0,
                }
            ]
            return
        if "where `brand_key` = %s or `brand_name` = %s" in normalized:
            full_payload = '{"brand_key":"리바로","data":{"payload":"' + ("x" * 6000) + '"}}'
            self.result = [
                {
                    "view_type": "strategic_ml",
                    "brand_key": "리바로",
                    "market_id": "ml_006",
                    "brand_name": "리바로",
                    "source": "ubist",
                    "measure": "sales",
                    "payload_size": len(full_payload),
                    "response_json_preview": full_payload,
                    "preview_note": "complete JSON sample",
                    "updated_at": "2026-05-21 11:00:00",
                }
            ]
            return
        if "response_json_preview" in normalized:
            if len(params) >= 9:
                view_type = params[6]
                source = params[7]
                measure = params[8] if "measure" in normalized else None
            else:
                view_type = params[0]
                source = params[1]
                measure = params[2] if "measure" in normalized and len(params) > 3 else None
            self.result = [
                {
                    "view_type": view_type,
                    "brand_key": "리바로" if "cache_cause" in normalized or "cache_deep_analysis" in normalized else None,
                    "market_id": "ml_006",
                    "brand_name": "리바로" if "cache_cause" in normalized or "cache_deep_analysis" in normalized else None,
                    "source": source,
                    "measure": measure,
                    "payload_size": 12_345,
                    "response_json_preview": '{"view":"' + view_type + '","data":{"kpi":1}}',
                    "preview_note": "complete JSON sample",
                    "updated_at": "2026-05-21 11:00:00",
                }
            ]
            return
        self.result = []
        self.one = None

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.one or {}


class _FakeSplitCacheConnection:
    def __init__(self) -> None:
        self.cursor_obj = _FakeSplitCacheCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self) -> None:
        self.closed = True


def test_layer_4_split_collectors_report_four_cache_entries(monkeypatch) -> None:
    monkeypatch.setattr(data_state_collector, "table_exists", lambda cur, table_name: True)

    def fake_schema(cur, table_name):
        common = [
            {"name": "view_type", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "source", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "response_json", "type": "longtext", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "payload_size", "type": "int", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "updated_at", "type": "timestamp", "nullable": True, "null_rate": 0.0, "unique_count": 1},
        ]
        if table_name == "cache_brands":
            return common
        if table_name == "cache_market_status":
            return [
                {"name": "market_id", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
                {"name": "measure", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
                {"name": "market_name", "type": "varchar", "nullable": True, "null_rate": 0.0, "unique_count": 1},
                *common,
            ]
        return [
            {"name": "brand_key", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "market_id", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "measure", "type": "varchar", "nullable": False, "null_rate": 0.0, "unique_count": 1},
            {"name": "brand_name", "type": "varchar", "nullable": True, "null_rate": 0.0, "unique_count": 1},
            {"name": "is_jw", "type": "tinyint", "nullable": True, "null_rate": 0.0, "unique_count": 1},
            *common,
        ]

    monkeypatch.setattr(
        data_state_collector,
        "get_db_schema",
        fake_schema,
    )
    monkeypatch.setattr(data_state_collector, "enrich_db_column_stats", lambda cur, table, schema, total, sample_limit=None: schema)
    monkeypatch.setattr(
        data_state_collector,
        "get_mart_storage_info",
        lambda cur, table_name: {"table_name": table_name, "engine": "InnoDB", "size_mb": 2.0},
    )
    conn = _FakeSplitCacheConnection()

    brands = collect_cache_brands(conn=conn, sample_limit_per_group=1, jw_limit_per_brand=1)
    market = collect_cache_market_status(conn=conn, sample_limit_per_group=1, jw_limit_per_brand=1)
    cause = collect_cache_cause(conn=conn, sample_limit_per_group=1, jw_limit_per_brand=1)
    deep = collect_cache_deep_analysis(conn=conn, sample_limit_per_group=1, jw_limit_per_brand=1)

    assert brands["logical_table"] == "cache_brands"
    assert market["logical_table"] == "cache_market_status"
    assert cause["logical_table"] == "cache_cause"
    assert deep["logical_table"] == "cache_deep_analysis"
    assert brands["total_rows"] == 6
    assert market["total_rows"] == 3038
    assert cause["total_rows"] == 137978
    assert deep["total_rows"] == 137978
    assert brands["layer"] == market["layer"] == cause["layer"] == deep["layer"] == "layer_4_cache"
    assert "cache_breakdown" in cause
    assert cause["sample_rows"][0]["response_json_preview"].startswith("{")
    assert cause["jw_deep_sample"]
    assert brands["sample_rows"][0]["truncated"] is True
    assert cause["sample_rows"][0]["truncated"] is True
    assert cause["jw_deep_sample"][0]["truncated"] is False
    assert cause["jw_deep_sample"][0]["is_jw_deep_sample"] is True
    assert len(cause["jw_deep_sample"][0]["response_json_preview"]) > 5_000
    assert "(+" not in cause["jw_deep_sample"][0]["response_json_preview"]


def test_data_dictionary_contains_split_cache_shapes() -> None:
    dictionary = viewer_builder.load_dictionary()

    assert "response_store" not in dictionary
    for table in ["cache_brands", "cache_market_status", "cache_cause", "cache_deep_analysis"]:
        assert table in dictionary
        assert dictionary[table]["layer"] == "layer_4_cache"
        assert "response_json" in dictionary[table]["columns"]
        assert "primary_key" in dictionary[table]
        assert "sample_interpretation" in dictionary[table]
    assert dictionary["cache_cause"]["join_target"]["table"] == "cache_market_status"
    assert set(dictionary["cache_cause"]["join_target"]["on"]) == {"view_type", "market_id", "source", "measure"}


def test_build_html_renders_layer_4_split_cache_sections(tmp_path: Path, monkeypatch) -> None:
    dictionary_path = tmp_path / "data_dictionary.json"
    dictionary_path.write_text(
        """
        {
          "_meta": {"version": "test"},
          "cache_brands": {
            "layer": "layer_4_cache",
            "purpose": "Brands cache",
            "row_grain": "view_type × source",
            "row_count_approx": "6",
            "primary_key": ["view_type", "source"],
            "columns": {
              "view_type": "view type",
              "source": "source",
              "response_json": "JSON response"
            },
            "sample_interpretation": {
              "brands": {
                "description": "Brand list response",
                "shape": {"brands": "[]"}
              }
            }
          },
          "cache_market_status": {
            "layer": "layer_4_cache",
            "purpose": "Market cache",
            "row_grain": "view_type × market_id × source × measure",
            "row_count_approx": "3,038",
            "primary_key": ["view_type", "market_id", "source", "measure"],
            "columns": {"response_json": "JSON response"},
            "sample_interpretation": {
              "market-status": {"shape": {"data": {"target_customer_competition": "{...}"}}}
            }
          },
          "cache_cause": {
            "layer": "layer_4_cache",
            "purpose": "Cause cache",
            "row_grain": "view_type × brand_key × market_id × source × measure",
            "row_count_approx": "137,978",
            "primary_key": ["view_type", "brand_key", "market_id", "source", "measure"],
            "join_target": {"table": "cache_market_status", "on": ["view_type", "market_id", "source", "measure"]},
            "columns": {"response_json": "JSON response"},
            "sample_interpretation": {
              "cause": {"shape": {"data": {"kpi": "{...}", "target_customer_competition": "served via JOIN"}}}
            }
          },
          "cache_deep_analysis": {
            "layer": "layer_4_cache",
            "purpose": "Deep-analysis cache",
            "row_grain": "view_type × brand_key × market_id × source × measure",
            "row_count_approx": "137,978",
            "primary_key": ["view_type", "brand_key", "market_id", "source", "measure"],
            "columns": {"response_json": "JSON response"},
            "sample_interpretation": {
              "deep-analysis": {"shape": {"data": {"forecast": "placeholder"}}}
            }
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(viewer_builder, "DICTIONARY_PATH", dictionary_path, raising=False)
    state = {
        "generated_at": "2026-05-21T11:30:00",
        "repo_commit": "113eaaa",
        "repo_tag": "",
        "total_rows": 3,
        "tables": {
            name: {
                "logical_table": name,
                "layer": "layer_4_cache",
                "purpose": "cache",
                "total_rows": rows,
                "total_columns": 3,
                "schema": [
                    {"name": "view_type", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "source", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "response_json_preview", "type": "longtext", "null_rate": 0.0, "unique_count": 1},
                ],
                "cache_breakdown": [
                    {"view_type": "strategic_ml", "source": "ubist", "measure": "sales", "row_count": rows, "size_mb": 1.5, "avg_kb": 768.0}
                ],
                "sample_rows": [
                    {
                        "view_type": "strategic_ml",
                        "source": "ubist",
                        "measure": "sales",
                        "response_json_preview": "{\"data\":{\"kpi\":1}}",
                    }
                ],
                "jw_deep_sample": [],
                "distribution": {},
                "storage_info": {"size_mb": 2.0},
            }
            for name, rows in {
                "cache_brands": 6,
                "cache_market_status": 3038,
                "cache_cause": 137978,
                "cache_deep_analysis": 137978,
            }.items()
        },
    }
    output_path = tmp_path / "viewer.html"

    viewer_builder.build_html(state, output_path)

    html = output_path.read_text(encoding="utf-8")
    assert "LAYER 4 CACHE" in html
    assert "cache_brands" in html
    assert "cache_market_status" in html
    assert "cache_cause" in html
    assert "cache_deep_analysis" in html
    assert html.count("response_store") == 0
    assert "layer_4_cache" in html
    assert "View/source/measure breakdown" in html
    assert "Response Shape Documentation" in html
    assert "target_customer_competition" in html
    assert "function openJsonModal" in html
    assert "json-cell-clickable" in html


def test_build_html_marks_truncated_l4_json_with_db_query_and_full_banner(tmp_path: Path) -> None:
    state = {
        "generated_at": "2026-05-21T14:40:00",
        "repo_commit": "eea040f",
        "repo_tag": "",
        "total_rows": 2,
        "tables": {
            "cache_cause": {
                "logical_table": "cache_cause",
                "layer": "layer_4_cache",
                "purpose": "cache",
                "total_rows": 2,
                "total_columns": 8,
                "schema": [
                    {"name": "view_type", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "brand_key", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "market_id", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "source", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "measure", "type": "varchar", "null_rate": 0.0, "unique_count": 1},
                    {"name": "response_json_preview", "type": "longtext", "null_rate": 0.0, "unique_count": 2},
                ],
                "sample_rows": [
                    {
                        "view_type": "strategic_ml",
                        "brand_key": "리바로",
                        "market_id": "ml_006",
                        "source": "ubist",
                        "measure": "sales",
                        "payload_size": 28_500,
                        "response_json_preview": "{\"brand_key\":\"리바로\",\"data\":{\"kpi\":1}",
                        "truncated": True,
                        "db_query": (
                            "SELECT response_json FROM cache_cause WHERE view_type = 'strategic_ml' "
                            "AND brand_key = '리바로' AND market_id = 'ml_006' "
                            "AND source = 'ubist' AND measure = 'sales';"
                        ),
                    }
                ],
                "jw_deep_sample": [
                    {
                        "view_type": "strategic_ml",
                        "brand_key": "리바로",
                        "market_id": "ml_006",
                        "source": "ubist",
                        "measure": "sales",
                        "payload_size": 28_500,
                        "response_json_preview": "{\"brand_key\":\"리바로\",\"data\":{\"kpi\":1,\"sources_data\":{\"metric_history\":{}}}}",
                        "truncated": False,
                        "is_jw_deep_sample": True,
                        "db_query": (
                            "SELECT response_json FROM cache_cause WHERE view_type = 'strategic_ml' "
                            "AND brand_key = '리바로' AND market_id = 'ml_006' "
                            "AND source = 'ubist' AND measure = 'sales';"
                        ),
                    }
                ],
                "distribution": {},
                "storage_info": {"size_mb": 1.0},
            }
        },
    }
    output_path = tmp_path / "viewer.html"

    build_html(state, output_path)

    html = output_path.read_text(encoding="utf-8")
    assert "Truncated" in html
    assert "Full response" in html
    assert "SELECT response_json FROM cache_cause WHERE" in html
    assert "view_type = 'strategic_ml'" in html
    assert "brand_key = '리바로'" in html
    assert "Copy SQL" in html
