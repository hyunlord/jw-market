from __future__ import annotations

import json
from pathlib import Path
import sys
import threading


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import warm_cache
from pipeline.scripts.api.catalog import DISPLAY_BRANDS


ROOT = Path(__file__).resolve().parents[2]


class FakeResponse:
    status = 200

    def __init__(self) -> None:
        self._chunks = [b'{"status":', b'"SUCCESS"}', b""]
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self._chunks.pop(0)


def test_warm_requests_posts_canonical_requests_and_streams_response_body() -> None:
    seen = []
    responses: list[FakeResponse] = []

    def open_request(request, timeout):
        seen.append((request, timeout))
        response = FakeResponse()
        responses.append(response)
        return response

    requests = [
        {"source": "ubist", "measure": "sales", "filters": {"atc4": ["C10A1"]}},
        {"source": "iqvia", "measure": "sales", "filters": {"atc4": ["N02B2"]}},
    ]

    results = warm_cache.warm_requests(
        base_url="http://jw-market-backend-api-test:8000",
        requests=requests,
        timeout_seconds=90,
        open_request=open_request,
        workers=1,
    )

    assert [result["status"] for result in results] == [200, 200]
    assert len(seen) == 2
    assert json.loads(seen[0][0].data) == requests[0]
    assert seen[0][0].full_url.endswith("/api/dynamic-market")
    assert seen[0][1] == 90
    assert all(response.read_sizes == [65_536, 65_536, 65_536] for response in responses)


def test_warm_requests_uses_three_workers() -> None:
    barrier = threading.Barrier(3)

    def open_request(_request, _timeout):
        barrier.wait(timeout=1.0)
        return FakeResponse()

    results = warm_cache.warm_requests(
        base_url="http://jw-market-backend-api-test:8000",
        requests=[{"request": index} for index in range(3)],
        timeout_seconds=90,
        open_request=open_request,
        workers=3,
    )

    assert [result["status"] for result in results] == [200, 200, 200]


def test_cache_cronjobs_maintain_then_warm_the_expected_service() -> None:
    manifests = {
        "dynamic-market-cache-warm-cronjob.yaml": "http://jw-market-backend-api-test-service",
        "dynamic-market-cache-warm-prod-cronjob.yaml": "http://jw-market-backend-api-service",
    }

    for filename, service_url in manifests.items():
        manifest = (ROOT / "deploy/k8s/jw-market" / filename).read_text()
        maintenance = manifest.index("pipeline.scripts.api.dynamic_market.cache_maintenance")
        warm = manifest.index("pipeline.scripts.api.dynamic_market.warm_cache")

        assert service_url in manifest
        assert maintenance < warm
        assert "--workers 3" in manifest
        assert "activeDeadlineSeconds: 1800" in manifest
        assert "name: DB_HOST" in manifest
        assert "llmops-mariadb-service.llmops.svc.cluster.local" in manifest
        assert "name: DB_PASSWORD" in manifest
        assert "name: galera-mariadb-galera" in manifest
        assert "key: mariadb-password" in manifest
        assert "name: DB_NAME" in manifest
        assert "jw_mart_d2_stage_20260630_r2" in manifest


def test_catalog_warm_set_covers_all_25_brands_and_168_strategic_variants() -> None:
    strategic = [item for item in warm_cache.DEFAULT_REQUESTS if str(item.get("view", "")).startswith("strategic_")]
    focus_brands = {str(item["filters"]["focus_brand_key"]) for item in strategic}

    assert len(DISPLAY_BRANDS) == 25
    assert len(strategic) == 168
    assert focus_brands == {brand.brand_name for brand in DISPLAY_BRANDS}
    assert {str(item["view"]) for item in strategic} == {"strategic_ml", "strategic_cd"}


def test_catalog_warm_set_uses_each_brand_source_measure_contract() -> None:
    strategic = [item for item in warm_cache.DEFAULT_REQUESTS if str(item.get("view", "")).startswith("strategic_")]

    for brand in DISPLAY_BRANDS:
        actual = {
            (str(item["view"]), str(item["source"]), str(item["measure"]))
            for item in strategic
            if item["filters"]["focus_brand_key"] == brand.brand_name
        }
        expected = {
            (view, source.lower(), measure)
            for view in ("strategic_ml", "strategic_cd")
            for source, measures in brand.available_measures.items()
            for measure in measures
        }
        assert actual == expected
