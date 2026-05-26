from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_CATALOG_USAGE_INTEGRATION") != "1",
    reason="set RUN_CATALOG_USAGE_INTEGRATION=1 for local catalog usage investigation",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = Path(os.getenv("CATALOG_USAGE_ARTIFACT_DIR", "/tmp/jw_catalog_investigation"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

EXPECTED_CATALOG_ROWS = {
    "ml_market": 16,
    "cd_market": 19,
    "cd_filter": 19,
    "strategic_brand": 4495,
    "strategic_product": 11865,
    "cd_brand": 2379,
    "cd_product": 4822,
}

ENDPOINT_CALLS: list[dict[str, Any]] = []


@pytest.fixture(scope="module")
def catalog_tracker():
    os.environ.setdefault("DB_HOST", "127.0.0.1")
    os.environ.setdefault("DB_PORT", "3308")
    os.environ.setdefault("DB_USER", "root")
    os.environ.setdefault("DB_PASSWORD", "")
    os.environ.setdefault("DB_NAME", "jw_mart")
    os.environ["CATALOG_TRACKER_ENABLED"] = "1"

    from pipeline.scripts.api import _catalog_tracker

    _catalog_tracker.install()
    _catalog_tracker.reset()
    return _catalog_tracker


@pytest.fixture(scope="module")
def client(catalog_tracker):
    del catalog_tracker
    from pipeline.scripts.api.main import app

    with TestClient(app) as test_client:
        yield test_client


def _summary(snapshot: dict[str, Any]) -> dict[str, int]:
    data = snapshot.get("summary", {})
    return {
        "parquet": int(data.get("parquet_read_count", 0)),
        "pyarrow": int(data.get("pyarrow_read_count", 0)),
        "catalog_sql": int(data.get("catalog_sql_count", 0)),
        "all_sql": int(data.get("all_sql_count", 0)),
    }


def _call(client: TestClient, tracker: Any, name: str, path: str, expected_status: int, params: dict[str, Any] | None = None):
    before = _summary(tracker.get_snapshot())
    response = client.get(path, params=params or {})
    after = _summary(tracker.get_snapshot())
    record = {
        "name": name,
        "path": path,
        "params": params or {},
        "status_code": response.status_code,
        "expected_status": expected_status,
        "delta": {key: after[key] - before[key] for key in after},
        "body_preview": response.text[:400],
    }
    ENDPOINT_CALLS.append(record)
    assert response.status_code == expected_status, record
    return response


def test_catalog_parquet_files_exist_with_expected_counts():
    observed = {}
    for table_name, expected_rows in EXPECTED_CATALOG_ROWS.items():
        path = REPO_ROOT / "output" / "catalog" / table_name / f"{table_name}.parquet"
        assert path.exists(), f"missing catalog parquet: {path}"
        observed[table_name] = pq.ParquetFile(path).metadata.num_rows
        assert observed[table_name] == expected_rows

    (ARTIFACT_DIR / "catalog_parquet_counts.json").write_text(
        json.dumps(observed, indent=2, ensure_ascii=False)
    )


def test_current_cache_split_endpoints(client: TestClient, catalog_tracker):
    calls = [
        ("health", "/api/health", {}, 200),
        ("brands_all", "/api/brands", {}, 200),
        ("brands_strategic_ml", "/api/brands", {"view": "strategic_ml"}, 200),
        ("brands_ubist", "/api/brands", {"source": "ubist"}, 200),
        (
            "market_status_ml006_ubist_sales",
            "/api/market-status/ml_006",
            {"view": "strategic_ml", "source": "ubist", "measure": "sales"},
            200,
        ),
        (
            "market_status_ml006_iqvia_sales",
            "/api/market-status/ml_006",
            {"view": "strategic_ml", "source": "iqvia", "measure": "sales"},
            200,
        ),
        (
            "cause_libaro_ubist_sales",
            "/api/cause/리바로",
            {"view": "strategic_ml", "source": "ubist", "measure": "sales"},
            200,
        ),
        (
            "cause_guardmet_iqvia_sales",
            "/api/cause/가드메트",
            {"view": "general", "source": "iqvia", "measure": "sales"},
            200,
        ),
        (
            "deep_libaro_ubist_sales",
            "/api/deep-analysis/리바로",
            {"view": "strategic_ml", "source": "ubist", "measure": "sales"},
            200,
        ),
        (
            "deep_guardmet_iqvia_sales",
            "/api/deep-analysis/가드메트",
            {"view": "general", "source": "iqvia", "measure": "sales"},
            200,
        ),
    ]
    for name, path, params, expected_status in calls:
        _call(client, catalog_tracker, name, path, expected_status, params=params)


def test_frontend_mockup_legacy_calls_are_currently_not_cache_split_contract(
    client: TestClient,
    catalog_tracker,
):
    legacy_calls = [
        ("legacy_market_status_no_params", "/api/market-status", {}, 422),
        (
            "legacy_cause_market_landscape_ubist",
            "/api/cause/가드메트",
            {"view": "market_landscape", "source": "UBIST", "measure": "sales"},
            422,
        ),
        ("legacy_deep_no_params", "/api/deep-analysis/가드메트", {}, 422),
    ]
    for name, path, params, expected_status in legacy_calls:
        _call(client, catalog_tracker, name, path, expected_status, params=params)


def test_z_tracker_final_snapshot(catalog_tracker):
    snapshot = catalog_tracker.get_snapshot()
    summary = snapshot["summary"]
    verdict = _catalog_verdict(
        parquet=int(summary["parquet_read_count"]),
        pyarrow=int(summary["pyarrow_read_count"]),
        catalog_sql=int(summary["catalog_sql_count"]),
    )
    payload = {
        "summary": summary,
        "verdict": verdict,
        "endpoint_calls": ENDPOINT_CALLS,
        "catalog_sql_queries": snapshot["catalog_sql_queries"],
        "parquet_reads": snapshot["parquet_reads"],
        "pyarrow_reads": snapshot["pyarrow_reads"],
        "all_sql_count": len(snapshot["all_sql_queries"]),
        "all_sql_samples": snapshot["all_sql_queries"][:20],
    }
    (ARTIFACT_DIR / "tracker_dump.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    (ARTIFACT_DIR / "endpoint_calls.json").write_text(json.dumps(ENDPOINT_CALLS, indent=2, ensure_ascii=False))
    (ARTIFACT_DIR / "test_summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    assert summary["installed"] is True
    assert summary["all_sql_count"] > 0, "tracker did not observe SQL; investigation would be inconclusive"
    assert summary["catalog_sql_count"] == 0
    assert summary["parquet_read_count"] == 0
    assert summary["pyarrow_read_count"] == 0


def _catalog_verdict(parquet: int, pyarrow: int, catalog_sql: int) -> str:
    has_file_reads = (parquet + pyarrow) > 0
    has_sql = catalog_sql > 0
    if has_file_reads and not has_sql:
        return "A (parquet only)"
    if has_sql and not has_file_reads:
        return "B (DB SELECT only)"
    if has_file_reads and has_sql:
        return "C (mixed)"
    return "D (no runtime catalog read; cache-only for tested endpoints)"

