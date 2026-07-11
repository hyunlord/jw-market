"""Warm high-frequency general dynamic-market combinations through the live API."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import json
import os
from typing import Any
from urllib.request import Request, urlopen


DEFAULT_REQUESTS: tuple[dict[str, Any], ...] = (
    {"source": "ubist", "measure": "sales", "filters": {"atc4": ["C10A1", "C10C"]}},
    {"source": "ubist", "measure": "sales", "filters": {"atc4": ["A10N1"]}},
    {"source": "ubist", "measure": "sales", "filters": {"atc4": ["N02B2"]}},
    {"source": "iqvia", "measure": "sales", "filters": {"atc4": ["C10A1", "C10C"]}},
    {"view": "strategic_ml", "source": "ubist", "measure": "sales", "filters": {"focus_brand_key": "리바로"}},
    {"view": "strategic_ml", "source": "iqvia", "measure": "sales", "filters": {"focus_brand_key": "리바로"}},
    {"view": "strategic_ml", "source": "iqvia", "measure": "sales", "filters": {"focus_brand_key": "마운자로"}},
    {
        "view": "strategic_cd",
        "source": "ubist",
        "measure": "sales",
        "filters": {"focus_brand_key": "리바로하이"},
    },
)


def warm_requests(
    *,
    base_url: str,
    requests: Iterable[dict[str, Any]],
    timeout_seconds: int,
    open_request: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    endpoint = f"{base_url.rstrip('/')}/api/dynamic-market"
    for payload in requests:
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with open_request(request, timeout=timeout_seconds) as response:
            body = response.read()
            results.append(
                {
                    "status": int(response.status),
                    "bytes": len(body),
                    "request": payload,
                }
            )
    return results


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
    args = parser.parse_args()
    results = warm_requests(
        base_url=args.base_url,
        requests=_requests_from_env(),
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(results, ensure_ascii=False, separators=(",", ":")))
    return 0 if all(result["status"] == 200 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
