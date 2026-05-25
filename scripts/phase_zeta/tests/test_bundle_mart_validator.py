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
        market_id, source_candidates, measure, brand = self.last_params
        key = (market_id, tuple(source_candidates), measure, brand)
        return self.rows.get(key)


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj


def _bundle(raw_value=100.0):
    return {
        "brand_context": {"name": "리바로"},
        "market_views": [
            {
                "view_id": "ML.UBIST.sales",
                "source": "UBIST",
                "measure": "sales",
                "market_meta": {"market_id_internal": "ml_006"},
                "target_brand_metric": {
                    "history": {"2026-04": {"raw_value": raw_value, "ms_pct": 10.0, "rank": 2}}
                },
                "competitors_top5": [],
            }
        ],
    }


def _mart_row(raw_value=100.0):
    return {
        "brand_name": "리바로",
        "metric_history": json.dumps({"2026-04": {"raw_value": raw_value, "ms": 10.0, "rank": 2}}),
        "value_recent": raw_value,
        "market_size_recent": 1000.0,
    }


def test_match_when_bundle_equals_mart():
    source_candidates = ("UBIST", "ubist")
    conn = FakeConnection({("ml_006", source_candidates, "sales", "리바로"): _mart_row()})

    result = validate_bundle_against_mart(_bundle(), conn, RunnerConfig.default_for_tests().validator)

    assert result["valid"]
    assert result["matched"] == result["total_checks"]


def test_mismatch_detected():
    source_candidates = ("UBIST", "ubist")
    conn = FakeConnection({("ml_006", source_candidates, "sales", "리바로"): _mart_row(raw_value=999.0)})

    result = validate_bundle_against_mart(_bundle(), conn, RunnerConfig.default_for_tests().validator)

    assert not result["valid"]
    assert any(item["field"] == "raw_value" for item in result["mismatched"])
