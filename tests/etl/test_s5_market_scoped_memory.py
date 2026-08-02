from __future__ import annotations

from pathlib import Path

import pandas as pd

from pipeline.etl.io.mart import strategic_cd, strategic_ml


def _disable_writes(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "ensure_json_columns", lambda *_args: None)
    monkeypatch.setattr(module, "delete_existing_rows", lambda *_args: None)
    monkeypatch.setattr(module, "insert_rows", lambda *_args: None)


def test_strategic_ml_loads_only_each_markets_scoped_sources(monkeypatch) -> None:
    markets = pd.DataFrame(
        [
            {"ml_id": "ml_ubist", "data_source": "ubist", "atc_codes_json": '["C10C"]'},
            {"ml_id": "ml_dual", "data_source": "both", "atc_codes_json": '["A10C1"]'},
        ]
    )
    brands = pd.DataFrame(
        [
            {"ml_id": "ml_ubist", "brand_id": "b1", "name": "one", "allowed_atc4_codes_json": '["C10C"]'},
            {"ml_id": "ml_dual", "brand_id": "b2", "name": "two", "allowed_atc4_codes_json": '["A10C1"]'},
        ]
    )
    monkeypatch.setattr(
        strategic_ml,
        "load_catalogs",
        lambda: (markets, brands, pd.DataFrame({"ml_id": []})),
    )
    observed_loads: list[tuple[str, frozenset[str]]] = []

    def load_general(source: str, atc4_codes: set[str] | None = None):
        assert atc4_codes
        observed_loads.append((source, frozenset(atc4_codes)))
        return [{"source": source, "scope": sorted(atc4_codes)}]

    observed_builds: list[tuple[str, list[str]]] = []

    def build_rows(row, _catalog_rows, general_rows, _context):
        observed_builds.append(
            (str(row["ml_id"]), [str(item["source"]) for item in general_rows])
        )
        return ([{"ml_id": row["ml_id"]}], [{"ml_id": row["ml_id"]}])

    monkeypatch.setattr(strategic_ml, "load_general_rows", load_general)
    monkeypatch.setattr(strategic_ml, "build_ml_rows", build_rows)
    monkeypatch.setattr(strategic_ml, "load_ubist_dimension_context", lambda *_args: {})
    monkeypatch.setattr(
        strategic_ml, "catalog_single_dimension_by_brand", lambda *_args: {}
    )
    _disable_writes(monkeypatch, strategic_ml)

    brand_rows, market_rows, stats = strategic_ml.compute_strategic_ml(
        False, True, Path("/unused")
    )

    assert [source for source, _scope in observed_loads] == [
        "ubist",
        "ubist",
        "iqvia_nsa",
    ]
    assert all(scope for _source, scope in observed_loads)
    assert observed_builds == [
        ("ml_ubist", ["ubist"]),
        ("ml_dual", ["ubist", "iqvia_nsa"]),
    ]
    assert len(brand_rows) == len(market_rows) == 2
    assert stats == {"brand_rows": 2, "market_rows": 2, "ml_count": 2}


def test_strategic_cd_loads_only_each_markets_scoped_sources(monkeypatch) -> None:
    markets = pd.DataFrame(
        [
            {
                "cd_id": "cd_ubist",
                "cd_filter_id": "f1",
                "data_source": "ubist",
            },
            {
                "cd_id": "cd_iqvia",
                "cd_filter_id": "f2",
                "data_source": "iqvia",
            },
        ]
    )
    brands = pd.DataFrame(
        [
            {"cd_id": "cd_ubist", "brand_id": "b1", "name": "one", "allowed_atc4_codes_json": '["C10C"]'},
            {"cd_id": "cd_iqvia", "brand_id": "b2", "name": "two", "allowed_atc4_codes_json": '["A10C1"]'},
        ]
    )
    filters = pd.DataFrame(
        [
            {"cd_filter_id": "f1", "atc4": '["C10C"]'},
            {"cd_filter_id": "f2", "atc4": '["A10C1"]'},
        ]
    )
    monkeypatch.setattr(strategic_cd, "load_catalogs", lambda: (markets, brands, filters))
    observed_loads: list[tuple[str, frozenset[str]]] = []

    def load_general(source: str, atc4_codes: set[str] | None = None):
        assert atc4_codes
        observed_loads.append((source, frozenset(atc4_codes)))
        return [{"source": source, "scope": sorted(atc4_codes)}]

    observed_builds: list[tuple[str, list[str]]] = []

    def build_rows(row, _catalog_rows, _filters, general_rows):
        observed_builds.append(
            (str(row["cd_id"]), [str(item["source"]) for item in general_rows])
        )
        return ([{"cd_market_id": row["cd_id"]}], [{"cd_market_id": row["cd_id"]}])

    monkeypatch.setattr(strategic_cd, "load_general_rows", load_general)
    monkeypatch.setattr(strategic_cd, "build_cd_rows", build_rows)
    _disable_writes(monkeypatch, strategic_cd)

    brand_rows, market_rows, stats = strategic_cd.compute_strategic_cd(
        False, True, Path("/unused")
    )

    assert [source for source, _scope in observed_loads] == ["ubist", "iqvia_nsa"]
    assert all(scope for _source, scope in observed_loads)
    assert observed_builds == [
        ("cd_ubist", ["ubist"]),
        ("cd_iqvia", ["iqvia_nsa"]),
    ]
    assert len(brand_rows) == len(market_rows) == 2
    assert stats == {
        "brand_rows": 2,
        "market_rows": 2,
        "cd_market_count": 2,
    }
