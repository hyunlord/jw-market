#!/usr/bin/env python3
"""Phase 29 validation: events import, Cut A/B, and SARIMAX POC."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25
    from pipeline.scripts.etl.phase29_events import (
        connect,
        ensure_events_raw_table,
        get_brand_events_cut_a,
        get_brand_events_cut_b,
        table_counts,
    )
    from pipeline.scripts.forecast.backtest import run_phase29_poc
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25
    from pipeline.scripts.etl.phase29_events import (
        connect,
        ensure_events_raw_table,
        get_brand_events_cut_a,
        get_brand_events_cut_b,
        table_counts,
    )
    from pipeline.scripts.forecast.backtest import run_phase29_poc


@dataclass
class Issue:
    kind: str
    detail: dict[str, Any]


def validate() -> dict[str, Any]:
    issues: list[Issue] = []
    conn = connect()
    try:
        ensure_events_raw_table(conn)
        counts = table_counts(conn)
        if counts["news_raw"] < 21_000:
            issues.append(Issue("news_raw_too_small", counts))
        if counts["events_raw"] != counts["news_raw"]:
            issues.append(Issue("events_raw_count_mismatch", counts))
        if counts["event_brand_scores"] < 46_000:
            issues.append(Issue("event_brand_scores_too_small", counts))

        cut_a_counts: dict[str, int] = {}
        cut_b_counts: dict[str, int] = {}
        for brand in sorted(CANONICAL_25):
            cut_a = get_brand_events_cut_a(conn, brand)
            cut_b = get_brand_events_cut_b(conn, brand)
            cut_a_counts[brand] = len(cut_a)
            cut_b_counts[brand] = len(cut_b)
            if len(cut_a) > 50:
                issues.append(Issue("cut_a_over_50", {"brand": brand, "count": len(cut_a)}))
            if brand != "플라주오피" and len(cut_a) < 5:
                issues.append(Issue("cut_a_under_5", {"brand": brand, "count": len(cut_a)}))
            for event in cut_b:
                if event.get("derivation") != "llm_direct" or int(event.get("score") or 0) < 80:
                    issues.append(Issue("cut_b_contract_violation", {"brand": brand, "event": event}))
    finally:
        conn.close()

    poc = run_phase29_poc(use_llm=False, persist=True)
    for brand, result in poc["brands"].items():
        for model_key in ("baseline", "with_llm"):
            metrics = result[model_key]["metrics"]
            for metric in ("rmse", "mape", "mae", "direction_acc"):
                value = metrics.get(metric)
                if value is None or value < 0:
                    issues.append(Issue("invalid_backtest_metric", {"brand": brand, "model": model_key, "metric": metric, "value": value}))

    return {
        "phase": "29",
        "validator": "events_cut_sarimax_poc",
        "counts": counts,
        "cut_a_counts": cut_a_counts,
        "cut_b_counts": cut_b_counts,
        "poc_verdicts": {brand: result["verdict"] for brand, result in poc["brands"].items()},
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
