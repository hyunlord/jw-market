from __future__ import annotations

import pytest

from pipeline.scripts.api.market_scope.archive_metrics import annual_ranking_payload
from pipeline.scripts.etl import build_cache_cause as cause


@pytest.mark.parametrize("label_key", ("brand", "company"))
def test_strategic_stacked_ranking_emits_contiguous_annual_prefix(label_key: str) -> None:
    period_map = {
        "2022-01": _rows((100, 90, 80, 70, 60, 50, 40)),
        "2023-01": _rows((100, 90, 80, 70, 60, 40, 50)),
    }

    result = cause._stacked_ranking(
        period_map,
        label_key=label_key,
        target_name="A",
    )

    for year_item in result["yearly"]:
        visible = [row for row in year_item["rankings"] if not row["is_others"]]
        assert [row["rank"] for row in visible] == list(range(1, len(visible) + 1))

        canonical = result["rankings_by_year"][str(year_item["year"])]
        assert [
            (row[label_key], row["rank"], row["value"], row["ms_pct"])
            for row in visible
        ] == [
            (row[label_key], row["rank"], row["value"], row["ms_pct"])
            for row in canonical[: len(visible)]
        ]

    rows_2022 = [row for row in result["yearly"][0]["rankings"] if not row["is_others"]]
    assert [row[label_key] for row in rows_2022] == ["A", "B", "C", "D", "E", "F", "G"]
    assert result["top_brands"] == ["A", "B", "C", "D", "E", "G", "기타"]


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
