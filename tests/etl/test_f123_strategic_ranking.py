from __future__ import annotations

import pytest

from pipeline.scripts.api.market_scope.archive_metrics import annual_ranking_payload
from pipeline.scripts.etl import build_cache_cause as cause


@pytest.mark.parametrize("label_key", ("brand", "company"))
def test_strategic_stacked_ranking_emits_visible_cohort_only(label_key: str) -> None:
    """D-1 (PL 2026-07-21, 방식 A): strategic cause stacked ranking now emits exactly the
    fixed visible cohort (선택 1 + 경쟁 top_n) + 기타, mirroring PATH1
    archive_metrics.annual_ranking_payload. The superseded F-123 contiguous-annual-prefix
    behavior pulled non-visible brands (here F, latest-year rank 7) into the yearly
    rankings, so the emitted ``series`` key set exceeded ``top_brands`` (series=11 vs
    top_brands=7 on live ml_006). The display contract now requires series == top_brands;
    trimmed brands fold into 기타 so the market total is preserved.
    """
    period_map = {
        "2022-01": _rows((100, 90, 80, 70, 60, 50, 40)),
        "2023-01": _rows((100, 90, 80, 70, 60, 40, 50)),
    }

    result = cause._stacked_ranking(
        period_map,
        label_key=label_key,
        target_name="A",
    )

    # top_brands unchanged (이미 정확): 선택 A + 경쟁 top-5(2023 순위) + 기타. F(2023 rank 7) 제외.
    assert result["top_brands"] == ["A", "B", "C", "D", "E", "G", "기타"]

    # yearly rankings = visible cohort only (F 미표시). 2022 실제 연간순위 유지(G=7위).
    rows_2022 = [row for row in result["yearly"][0]["rankings"] if not row["is_others"]]
    assert [row[label_key] for row in rows_2022] == ["A", "B", "C", "D", "E", "G"]
    assert [row["rank"] for row in rows_2022] == [1, 2, 3, 4, 5, 7]

    # ★ R-2 / G-1: series 키 집합 == top_brands. 확장 집합(F 포함)이 재발하면 실패.
    assert set(result["series"]) == set(result["top_brands"])
    assert "F" not in result["series"]

    # ★ V-5 총합 보존: 기타가 trim 된 비가시 브랜드를 흡수해 연도별 시장총액이 원천과 일치.
    for year_key, period_rows in (("2022", period_map["2022-01"]), ("2023", period_map["2023-01"])):
        year_item = next(it for it in result["yearly"] if str(it["year"]) == year_key)
        emitted_total = sum(
            float(row["value"]) for row in year_item["rankings"] if row.get("value") is not None
        )
        source_total = sum(float(r["value"]) for r in period_rows)
        assert emitted_total == pytest.approx(source_total)


def _rows(values: tuple[int, ...]) -> list[dict[str, object]]:
    names = ("A", "B", "C", "D", "E", "F", "G")
    return [
        {
            "brand": name,
            "company": name,
            "value": float(value),
            "is_jw": name == "A",
        }
        for name, value in zip(names, values, strict=True)
    ]


def test_market_scope_recompute_restores_visible_only_series() -> None:
    """F-134: the strategic competition Tracker emits only the selected brand plus its
    top competitors (visible_ids = select+5), NOT the F-123 contiguous rank prefix.

    Non-visible brands (here F, ranked 7th in the latest year) must never be pulled into
    the tracker series just because they occupy an intermediate rank in an earlier year.
    cause_ranking continuity (contiguous prefix, golden 74624725) is intentionally kept
    separate and is covered by test_strategic_stacked_ranking_emits_contiguous_annual_prefix.
    """
    histories = {
        name: {"2022-01": float(old), "2023-01": float(new)}
        for name, old, new in zip(
            ("A", "B", "C", "D", "E", "F", "G"),
            (100, 90, 80, 70, 60, 50, 40),
            (100, 90, 80, 70, 60, 40, 50),
            strict=True,
        )
    }

    result = annual_ranking_payload(histories, label_key="brand_key", focus_id="A")

    # visible_ids = latest-year (2023) top-6 with focus first = [A, B, C, D, E, G]; F (2023 rank 7) excluded.
    rows_2022 = [row for row in result["yearly"][0]["rankings"] if not row["is_others"]]
    assert [row["brand_key"] for row in rows_2022] == ["A", "B", "C", "D", "E", "G"]
    # 2022 ranks of the visible set: G is rank 7 in 2022; the non-visible rank-6 brand (F) is not shown.
    assert [row["rank"] for row in rows_2022] == [1, 2, 3, 4, 5, 7]
    assert result["top_brands"] == ["A", "B", "C", "D", "E", "G", "기타"]
    # G-1 / G-6: series is exactly the 6 visible + 기타; the widened emitted set (incl. F) must not return.
    assert set(result["series"]) == {"A", "B", "C", "D", "E", "G", "기타"}
    assert "F" not in result["series"]


@pytest.mark.parametrize("label_key", ("brand_key", "company"))
def test_market_scope_recompute_preserves_selected_real_zero(label_key: str) -> None:
    histories = {
        "A": {"2022-01": 100.0, "2023-01": 100.0},
        "B": {"2022-01": 0.0, "2023-01": 90.0},
        "C": {"2022-01": 30.0, "2023-01": 80.0},
    }

    result = annual_ranking_payload(histories, label_key=label_key, focus_id="A")

    rows_2022 = [row for row in result["yearly"][0]["rankings"] if not row["is_others"]]
    real_zero = next(row for row in rows_2022 if row[label_key] == "B")
    assert real_zero["rank"] is None
    assert real_zero["value"] == 0.0
    assert real_zero["ms_pct"] == 0.0
