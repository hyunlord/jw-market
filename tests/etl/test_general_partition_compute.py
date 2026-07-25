from __future__ import annotations

import json

import pandas as pd
import pytest

from pipeline.etl.io.mart import general_compute
from pipeline.etl.io.mart.general_rows import build_brand_rows


def _base_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "product_code": "p1",
                "product_name": "Product One",
                "brand_name": "Brand One",
                "brand_key": "brandone",
                "atc4_code": "C10A1",
                "atc4_desc": "C10A1 Test",
                "period_yyyymm": "2026-01",
                "channel": "의원",
                "specialty": "가정의학과",
                "manufacturer": "Maker",
                "company": "Seller",
                "audit_code": "p1",
                "display_priority_value_minor": 1000,
                "raw_sales": 10.0,
                "raw_volume": 2.0,
                "raw_sales_minor": 1000,
                "raw_volume_minor": 200,
                "source": "ubist",
            }
        ]
    )


def _without_computed_at(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized = []
    for row in rows:
        item = json.loads(json.dumps(row, ensure_ascii=False))
        if isinstance(item.get("payload"), dict):
            item["payload"].pop("computed_at", None)
        normalized.append(item)
    return normalized


def test_value_column_path_matches_legacy_measure_frame() -> None:
    base = _base_frame()
    legacy = base.copy()
    legacy["measure"] = "sales"
    legacy["raw_value"] = legacy["raw_sales"]

    expected = build_brand_rows("ubist", "sales", legacy, {})
    actual = build_brand_rows("ubist", "sales", base, {}, value_column="raw_sales")

    assert _without_computed_at(actual) == _without_computed_at(expected)


def test_ubist_compute_streams_each_atc_without_bulk_loader_or_measure_copy(
    monkeypatch,
    tmp_path,
) -> None:
    frames = [
        ("A01A1", _base_frame().assign(atc4_code="A01A1")),
        ("C10A1", _base_frame()),
    ]
    monkeypatch.setattr(general_compute, "load_catalog_key_map", lambda: {})
    monkeypatch.setattr(
        general_compute,
        "iter_ubist_atc4_worksets",
        lambda **_kwargs: iter(frames),
    )
    monkeypatch.setattr(
        general_compute,
        "load_ubist_base_frame",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("bulk loader used")),
    )
    monkeypatch.setattr(
        general_compute,
        "ubist_measure_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("measure copy used")),
    )

    brand_rows, market_rows, stats = general_compute.compute_general(
        "ubist",
        dry_run=True,
        output_dir=tmp_path,
    )

    assert len(brand_rows) == 1
    assert len(market_rows) == 1
    assert stats["return_mode"] == "streamed_preview"
    assert stats["output_paths"] == {
        "brand": str(tmp_path / "general_v3_ubist_brand_rows.jsonl"),
        "market": str(tmp_path / "general_v3_ubist_market_rows.jsonl"),
    }
    assert stats["brand_rows"] == 4
    assert stats["market_rows"] == 4
    assert len((tmp_path / "general_v3_ubist_brand_rows.jsonl").read_text().splitlines()) == 4
    assert len((tmp_path / "general_v3_ubist_market_rows.jsonl").read_text().splitlines()) == 4


def test_non_partitioned_dry_run_writes_canonical_output(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(general_compute, "load_catalog_key_map", lambda: {})
    monkeypatch.setattr(general_compute, "load_iqvia_base_frame", lambda **_kwargs: object())
    monkeypatch.setattr(
        general_compute,
        "iqvia_measure_frame",
        lambda _base, _measure: _base_frame().assign(raw_value=10.0),
    )

    brand_rows, market_rows, stats = general_compute.compute_general(
        "iqvia_nsa",
        dry_run=True,
        output_dir=tmp_path,
        limit_atc4=1,
    )

    assert brand_rows
    assert market_rows
    assert stats["output_paths"] == {
        "brand": str(tmp_path / "general_v3_iqvia_nsa_brand_rows.jsonl"),
        "market": str(tmp_path / "general_v3_iqvia_nsa_market_rows.jsonl"),
    }
    assert (tmp_path / "general_v3_iqvia_nsa_brand_rows.jsonl").read_text()
    assert (tmp_path / "general_v3_iqvia_nsa_market_rows.jsonl").read_text()


def test_insert_replaces_rows_only_after_all_partitions_are_staged(
    monkeypatch,
) -> None:
    events: list[str] = []

    def partitions(**_kwargs):
        events.append("spool:first")
        yield "C10A1", _base_frame()
        events.append("spool:second")
        yield "C10A2", _base_frame().assign(atc4_code="C10A2")

    monkeypatch.setattr(general_compute, "load_catalog_key_map", lambda: {})
    monkeypatch.setattr(general_compute, "iter_ubist_atc4_worksets", partitions)
    monkeypatch.setattr(general_compute, "ensure_json_columns", lambda *_args: None)
    monkeypatch.setattr(
        general_compute,
        "replace_source_rows_from_jsonl",
        lambda **_kwargs: events.append("replace"),
    )

    general_compute.compute_general("ubist", insert=True)

    assert events == ["spool:first", "spool:second", "replace"]


def test_later_partition_failure_preserves_existing_rows(
    monkeypatch,
) -> None:
    events: list[str] = []

    def partitions(**_kwargs):
        yield "C10A1", _base_frame()
        raise RuntimeError("second partition failed")

    monkeypatch.setattr(general_compute, "load_catalog_key_map", lambda: {})
    monkeypatch.setattr(general_compute, "iter_ubist_atc4_worksets", partitions)
    monkeypatch.setattr(general_compute, "ensure_json_columns", lambda *_args: None)
    monkeypatch.setattr(
        general_compute,
        "replace_source_rows_from_jsonl",
        lambda **_kwargs: events.append("replace"),
    )

    with pytest.raises(RuntimeError, match="second partition failed"):
        general_compute.compute_general("ubist", insert=True)

    assert events == []


def test_empty_partition_iterator_fails_before_delete(
    monkeypatch,
) -> None:
    events: list[str] = []

    monkeypatch.setattr(general_compute, "load_catalog_key_map", lambda: {})
    monkeypatch.setattr(general_compute, "iter_ubist_atc4_worksets", lambda **_kwargs: iter(()))
    monkeypatch.setattr(general_compute, "ensure_json_columns", lambda *_args: None)
    monkeypatch.setattr(
        general_compute,
        "delete_source_rows",
        lambda *_args: events.append("delete"),
    )

    with pytest.raises(RuntimeError, match="no UBIST ATC4 partitions"):
        general_compute.compute_general("ubist", insert=True)

    assert events == []
