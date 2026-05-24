#!/usr/bin/env python3
"""Single-news workflow 196 smoke test without writing score markers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import requests

from score_v2 import build_payload, load_catalog, parse_workflow_response, validate_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--genos-url", default=os.environ.get("GENOS_URL", ""))
    parser.add_argument("--genos-token", default=os.environ.get("GENOS_TOKEN", ""))
    parser.add_argument("--workflow-id", type=int, default=int(os.environ.get("WORKFLOW_ID", "196")))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("TIMEOUT_SEC", "60")))
    args = parser.parse_args()

    if not args.genos_url or not args.genos_token:
        raise RuntimeError("GENOS_URL and GENOS_TOKEN are required")

    catalog, catalog_sha1, jw25 = load_catalog(args.catalog)
    news = json.loads(args.news.read_text(encoding="utf-8"))
    payload = build_payload(news, catalog, args.workflow_id)
    started = time.time()
    response = requests.post(
        f"{args.genos_url.rstrip('/')}/api/gateway/workflow/{args.workflow_id}/run/v2",
        json=payload,
        headers={"Authorization": f"Bearer {args.genos_token}"},
        timeout=args.timeout_sec,
    )
    duration = time.time() - started
    response.raise_for_status()
    raw = response.json()
    parsed, usage = parse_workflow_response(raw)
    validated = validate_result(parsed, jw25)
    audit = {
        "test_news_path": str(args.news),
        "workflow_id": args.workflow_id,
        "catalog_sha1": catalog_sha1,
        "request_sha1": hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "duration_sec": duration,
        "validation": {
            "http_status": response.status_code,
            "response_is_json": isinstance(raw, dict),
            "matches_exists": isinstance(validated.get("matches"), list),
            "matches_count": len(validated.get("matches", [])),
            "matches_only_jw25": all(m["drug"] in jw25 for m in validated.get("matches", [])),
            "reason_field_filled": all(bool(m.get("reason", "").strip()) for m in validated.get("matches", [])),
            "scores_in_range": all(0 <= m.get("score", -1) <= 100 for m in validated.get("matches", [])),
            "has_summary": bool(validated.get("summary")),
            "has_tag": bool(validated.get("tag")),
            "tag_in_6_categories": validated.get("tag")
            in {"신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"},
        },
        "result": validated,
        "usage": usage,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit["validation"], ensure_ascii=False, indent=2))
    return 0 if all(audit["validation"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
