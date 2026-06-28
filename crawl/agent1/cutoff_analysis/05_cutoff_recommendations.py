#!/usr/bin/env python3
"""Recommend frontend panel and graph marker score cutoffs from generated samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import SAMPLES_DIR, describe_counts, ensure_dirs


PANEL_CANDIDATES = [20, 25, 30, 35]
MARKER_CANDIDATES = [60, 70, 75, 80]


def panel_stats(matrix: pd.DataFrame, cutoff: int) -> dict[str, object]:
    col = f"ge_{cutoff}"
    jw = matrix[matrix["brand_group"] == "jw"]
    all_total = int(matrix[col].sum())
    return {
        "cutoff": cutoff,
        "global_total_events": all_total,
        "jw": describe_counts(jw[col]),
        "jw_zero_brands": int((jw[col] == 0).sum()),
        "jw_brand_count": int(len(jw)),
    }


def marker_stats(marker_summary: dict[str, dict[str, float | int]], cutoff: int) -> dict[str, object]:
    stats = marker_summary[str(cutoff)]
    return {"cutoff": cutoff, **stats}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(SAMPLES_DIR / "cutoff_scenarios.json"))
    args = parser.parse_args()

    ensure_dirs()
    matrix = pd.read_csv(SAMPLES_DIR / "brand_cutoff_5y.csv")
    marker_summary = json.loads((SAMPLES_DIR / "marker_density_summary.json").read_text(encoding="utf-8"))

    panel = {str(c): panel_stats(matrix, c) for c in PANEL_CANDIDATES}
    marker = {str(c): marker_stats(marker_summary, c) for c in MARKER_CANDIDATES}
    scenarios = {
        "cutoff_1_frontend_panel": {
            "recommended": {
                **panel["25"],
                "reason": "5년 전체에서 JW 브랜드별 이벤트 수가 충분히 남고, 20점대 이하의 약한 매칭 노이즈를 일부 줄이는 균형점입니다.",
            },
            "alternatives": [
                {**panel["20"], "trade_off": "이벤트 풍부성이 가장 좋지만 20점대 side mention 노이즈가 더 많이 노출됩니다."},
                {**panel["30"], "trade_off": "패널 노이즈가 줄지만 작은 브랜드의 이벤트 수가 더 빠르게 줄어듭니다."},
                {**panel["35"], "trade_off": "더 정선된 패널이 되지만 low-volume 브랜드에서 빈 구간 위험이 커집니다."},
            ],
        },
        "cutoff_2_graph_marker": {
            "recommended": {
                **marker["75"],
                "reason": "최근 1년 JW 월별 평균 marker가 1건 안팎으로 내려가 그래프 overcrowding을 줄이면서, 25개 JW 중 22개 브랜드는 marker를 확보합니다.",
            },
            "alternatives": [
                {**marker["60"], "trade_off": "이슈 포착은 풍부하지만 월별 평균 6건 이상/brand로 marker 전용 UI에는 과밀합니다."},
                {**marker["70"], "trade_off": "과밀은 줄지만 아직 월별 평균 2.9건/brand라 밀도 목표보다 높습니다."},
                {**marker["80"], "trade_off": "overlap은 가장 안정적이지만 1년 내 marker 없는 JW 브랜드가 9개로 늘어납니다."},
            ],
        },
    }
    Path(args.output).write_text(json.dumps(scenarios, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"panel_recommended": 25, "marker_recommended": 75}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
