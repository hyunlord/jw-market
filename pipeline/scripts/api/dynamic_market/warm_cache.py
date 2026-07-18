"""Warm high-frequency general dynamic-market combinations through the live API."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
import json
import os
from typing import Any
from urllib.request import Request, urlopen

from pipeline.scripts.api.catalog import DISPLAY_BRANDS


GENERAL_REQUESTS: tuple[dict[str, Any], ...] = (
    {"source": "ubist", "measure": "sales", "filters": {"atc4": ["C10A1", "C10C"]}},
    {"source": "ubist", "measure": "sales", "filters": {"atc4": ["A10N1"]}},
    {"source": "ubist", "measure": "sales", "filters": {"atc4": ["N02B2"]}},
    {"source": "iqvia", "measure": "sales", "filters": {"atc4": ["C10A1", "C10C"]}},
)


def catalog_warm_requests() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "view": view,
            "source": source.lower(),
            "measure": measure,
            "filters": {"focus_brand_key": brand.brand_name},
        }
        for brand in DISPLAY_BRANDS
        for view in ("strategic_ml", "strategic_cd")
        for source, measures in brand.available_measures.items()
        for measure in measures
    )


DEFAULT_REQUESTS = GENERAL_REQUESTS + catalog_warm_requests()


def _warm_one(
    *,
    endpoint: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    open_request: Callable[..., Any],
) -> dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    byte_count = 0
    with open_request(request, timeout=timeout_seconds) as response:
        while True:
            chunk = response.read(65_536)
            if not chunk:
                break
            byte_count += len(chunk)
        return {"status": int(response.status), "bytes": byte_count, "request": payload}


def warm_requests(
    *,
    base_url: str,
    requests: Iterable[dict[str, Any]],
    timeout_seconds: int,
    open_request: Callable[..., Any] = urlopen,
    workers: int = 3,
) -> list[dict[str, Any]]:
    endpoint = f"{base_url.rstrip('/')}/api/dynamic-market"
    payloads = list(requests)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(
            executor.map(
                lambda payload: _warm_one(
                    endpoint=endpoint,
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                    open_request=open_request,
                ),
                payloads,
            )
        )


def _requests_from_env() -> list[dict[str, Any]]:
    raw = os.getenv("DYNAMIC_WARM_REQUESTS_JSON", "").strip()
    if not raw:
        return [dict(item) for item in DEFAULT_REQUESTS]
    payload = json.loads(raw)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("DYNAMIC_WARM_REQUESTS_JSON must be a JSON array of objects")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.getenv("DYNAMIC_WARM_BASE_URL", "http://jw-market-backend-api-test:8000"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--workers", type=int, default=int(os.getenv("DYNAMIC_WARM_WORKERS", "3")))
    args = parser.parse_args()
    results = warm_requests(
        base_url=args.base_url,
        requests=_requests_from_env(),
        timeout_seconds=args.timeout_seconds,
        workers=args.workers,
    )
    print(json.dumps(results, ensure_ascii=False, separators=(",", ":")))
    return 0 if all(result["status"] == 200 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
