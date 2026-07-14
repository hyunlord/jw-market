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


def test_market_scope_recompute_uses_the_same_contiguous_annual_prefix() -> None:
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

    rows_2022 = [row for row in result["yearly"][0]["rankings"] if not row["is_others"]]
    assert [row["rank"] for row in rows_2022] == [1, 2, 3, 4, 5, 6, 7]
    assert [row["brand_key"] for row in rows_2022] == ["A", "B", "C", "D", "E", "F", "G"]
    assert result["top_brands"] == ["A", "B", "C", "D", "E", "G", "기타"]
    assert "F" in result["series"]


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
