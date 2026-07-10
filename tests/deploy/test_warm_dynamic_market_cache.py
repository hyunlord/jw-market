from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import warm_cache


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return b'{"status":"SUCCESS"}'


def test_warm_requests_posts_sequential_canonical_requests() -> None:
    seen = []

    def open_request(request, timeout):
        seen.append((request, timeout))
        return FakeResponse()

    requests = [
        {"source": "ubist", "measure": "sales", "filters": {"atc4": ["C10A1"]}},
        {"source": "iqvia", "measure": "sales", "filters": {"atc4": ["N02B2"]}},
    ]

    results = warm_cache.warm_requests(
        base_url="http://jw-market-backend-api-test:8000",
        requests=requests,
        timeout_seconds=90,
        open_request=open_request,
    )

    assert [result["status"] for result in results] == [200, 200]
    assert len(seen) == 2
    assert json.loads(seen[0][0].data) == requests[0]
    assert seen[0][0].full_url.endswith("/api/dynamic-market")
    assert seen[0][1] == 90
