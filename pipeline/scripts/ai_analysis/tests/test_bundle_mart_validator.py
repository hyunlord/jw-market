from __future__ import annotations

import json

from phase_zeta_runner.bundle_mart_validator import validate_bundle_against_mart
from phase_zeta_runner.config import RunnerConfig


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.last_params = None
        self.description = None

    def execute(self, _sql, params):
        self.last_params = params

    def fetchone(self):
        if len(self.last_params) == 4:
            market_id, source_candidates, measure, brand = self.last_params
            key = ("brand", market_id, tuple(source_candidates), measure, brand)
            return self.rows.get(key)
        market_id, source_candidates, measure = self.last_params
        key = ("market", market_id, tuple(source_candidates), measure)
        return self.rows.get(key)


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj


def _bundle(raw_value=100.0, ms_pct=10.0, rank=2, competitor=None, brand_name="리바로"):
    return {
        "brand_context": {"name": brand_name},
        "market_views": [
            {
                "view_id": "ML.UBIST.sales",
                "source": "UBIST",
                "measure": "sales",
                "market_meta": {"market_id_internal": "ml_006"},
                "target_brand_metric": {
                    "history": {"2026-04": {"raw_value": raw_value, "ms_pct": ms_pct, "rank": rank}}
                },
                "competitors_top5": [competitor] if competitor else [],
            }
        ],
    }


def _mart_row(brand_name="리바로", raw_value=100.0, ms=10.0, rank=2, period="2026-04"):
    return {
        "brand_name": brand_name,
        "metric_history": json.dumps({period: {"raw_value": raw_value, "ms": ms, "rank": rank}}),
        "value_recent": raw_value,
        "market_size_recent": 1000.0,
    }


def _market_row(market_size=1000.0, period="2026-04"):
    return {"market_size_series": json.dumps({period: market_size})}


def test_match_when_bundle_equals_mart():
    source_candidates = ("UBIST", "ubist")
    conn = FakeConnection(
        {
            ("brand", "ml_006", source_candidates, "sales", "리바로"): _mart_row(),
            ("market", "ml_006", source_candidates, "sales"): _market_row(),
        }
    )

    result = validate_bundle_against_mart(_bundle(), conn, RunnerConfig.default_for_tests().validator)

    assert result["valid"]
    assert result["matched"] == result["total_checks"]


def test_mismatch_detected():
    source_candidates = ("UBIST", "ubist")
    conn = FakeConnection(
        {
            ("brand", "ml_006", source_candidates, "sales", "리바로"): _mart_row(raw_value=999.0),
            ("market", "ml_006", source_candidates, "sales"): _market_row(),
        }
    )

    result = validate_bundle_against_mart(_bundle(), conn, RunnerConfig.default_for_tests().validator)

    assert not result["valid"]
    assert any(item["field"] == "raw_value" for item in result["mismatched"])


def test_canonical_ms_match_when_sum_lt_market_size():
    source_candidates = ("IQVIA", "iqvia_nsa", "iqvia")
    bundle = {
        "brand_context": {"name": "악템라"},
        "market_views": [
            {
                "view_id": "ML.IQVIA.sales",
                "source": "IQVIA",
                "measure": "sales",
                "market_meta": {"market_id_internal": "ml_011"},
                "target_brand_metric": {
                    "history": {
                        "2024-Q3": {
                            "raw_value": 5298005624.0,
                            "ms_pct": 4.888337,
                            "rank": 1,
                        }
                    }
                },
                "competitors_top5": [],
            }
        ],
    }
    conn = FakeConnection(
        {
            ("brand", "ml_011", source_candidates, "sales", "악템라"): _mart_row(
                brand_name="악템라",
                raw_value=5298005624.0,
                ms=4.999929,
                rank=1,
                period="2024-Q3",
            ),
            ("market", "ml_011", source_candidates, "sales"): _market_row(
                market_size=108380528436.0,
                period="2024-Q3",
            ),
        }
    )

    result = validate_bundle_against_mart(bundle, conn, RunnerConfig.default_for_tests().validator)

    assert result["valid"]
    assert not result["mismatched"]


def test_canonical_ms_mismatch_when_raw_data_truly_different():
    source_candidates = ("UBIST", "ubist")
    conn = FakeConnection(
        {
            ("brand", "ml_006", source_candidates, "sales", "리바로"): _mart_row(raw_value=200.0, ms=20.0),
            ("market", "ml_006", source_candidates, "sales"): _market_row(market_size=1000.0),
        }
    )

    result = validate_bundle_against_mart(_bundle(raw_value=100.0, ms_pct=10.0), conn, RunnerConfig.default_for_tests().validator)

    assert not result["valid"]
    assert any(item["field"] == "raw_value" for item in result["mismatched"])
    assert any(item["field"] == "ms" for item in result["mismatched"])


def test_market_size_unavailable_skips_ms_check():
    source_candidates = ("UBIST", "ubist")
    conn = FakeConnection(
        {
            ("brand", "ml_006", source_candidates, "sales", "리바로"): _mart_row(raw_value=100.0, ms=999.0),
        }
    )

    result = validate_bundle_against_mart(_bundle(raw_value=100.0, ms_pct=10.0), conn, RunnerConfig.default_for_tests().validator)

    assert result["valid"]
    assert not result["mismatched"]


def test_competitor_ms_uses_canonical_market_size():
    source_candidates = ("UBIST", "ubist")
    competitor = {
        "brand_name": "경쟁약",
        "history": {"2026-04": {"raw_value": 50.0, "ms_pct": 5.0, "rank": 3}},
    }
    conn = FakeConnection(
        {
            ("brand", "ml_006", source_candidates, "sales", "리바로"): _mart_row(raw_value=100.0, ms=10.0),
            ("brand", "ml_006", source_candidates, "sales", "경쟁약"): _mart_row(
                brand_name="경쟁약",
                raw_value=50.0,
                ms=7.0,
                rank=3,
            ),
            ("market", "ml_006", source_candidates, "sales"): _market_row(market_size=1000.0),
        }
    )

    result = validate_bundle_against_mart(
        _bundle(raw_value=100.0, ms_pct=10.0, competitor=competitor),
        conn,
        RunnerConfig.default_for_tests().validator,
    )

    assert result["valid"]
    assert not result["mismatched"]
