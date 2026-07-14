from __future__ import annotations

import pytest

from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator
from pipeline.scripts.api.dynamic_market.cause_ranking import brand_ranking, company_hhi_series, company_ranking
from pipeline.scripts.api.dynamic_market.types import BrandMetric, BrandRef
def test_general_metric_loader_reads_company_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a normal general-view metric request.
    captured_sql = ""

    def fake_iter_rows(sql: str, _params: tuple[object, ...]):
        nonlocal captured_sql
        captured_sql = sql
        return iter(())

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)
    aggregator = MetricAggregator(mart_db="jw_mart")

    # When: the runtime metric rows are loaded.
    tuple(
        aggregator._iter_metric_rows(
            brands=(BrandRef("a", "A", "B02D1"),),
            source="iqvia_nsa",
            measure="sales",
            channel_axis=None,
        )
    )

    # Then: company metadata is selected with the metric values.
    assert "by_dimension" in captured_sql


def test_company_ranking_aggregates_brands_by_mart_company() -> None:
    # Given: two brands owned by one company and a third brand owned by another.
    brands = (
        _brand("a", "Brand A", "Company X", {"2023-Q1": 40.0}),
        _brand("b", "Brand B", "Company X", {"2023-Q1": 30.0}),
        _brand("c", "Brand C", "Company Y", {"2023-Q1": 30.0}),
    )

    # When: the company tracker ranking is built.
    result = company_ranking(brands)

    # Then: brands sharing a company become one company row.
    rows = result["rankings_by_year"]["2023"]
    assert [(row["company"], row["value"], row["rank"]) for row in rows] == [
        ("Company X", 70.0, 1),
        ("Company Y", 30.0, 2),
    ]


def test_company_hhi_aggregates_brands_before_computing_concentration() -> None:
    # Given: two brands owned by one company and one brand owned by another.
    quarters = ("2023-Q1", "2023-Q2", "2023-Q3", "2023-Q4")
    brands = (
        _brand("a", "Brand A", "Company X", {quarter: 40.0 for quarter in quarters}),
        _brand("b", "Brand B", "Company X", {quarter: 30.0 for quarter in quarters}),
        _brand("c", "Brand C", "Company Y", {quarter: 30.0 for quarter in quarters}),
    )

    # When: company concentration is calculated for the complete IQVIA year.
    result = company_hhi_series(brands, source="iqvia_nsa")

    # Then: HHI is based on the two company shares (70/30), not three brands.
    assert result == [{"period": "2023", "period_full": "2023", "year": 2023, "hhi": 5800.0}]


def test_brand_ranking_includes_intervening_actual_ranks_for_fixed_competitors() -> None:
    # Given: total-window selection keeps brand G although brand F outranks it in 2023.
    brands = (
        _brand("a", "A", "A Co", {"2023-Q1": 100.0, "2024-Q1": 100.0}),
        _brand("b", "B", "B Co", {"2023-Q1": 90.0, "2024-Q1": 90.0}),
        _brand("c", "C", "C Co", {"2023-Q1": 80.0, "2024-Q1": 80.0}),
        _brand("d", "D", "D Co", {"2023-Q1": 70.0, "2024-Q1": 70.0}),
        _brand("e", "E", "E Co", {"2023-Q1": 60.0, "2024-Q1": 60.0}),
        _brand("f", "F", "F Co", {"2023-Q1": 50.0, "2024-Q1": 0.0}),
        _brand("g", "G", "G Co", {"2023-Q1": 40.0, "2024-Q1": 100.0}),
    )

    # When: the selected brand plus five fixed competitors are rendered for 2023.
    result = brand_ranking(brands, focus=brands[0])

    # Then: the actual sixth-place brand is not hidden merely because the fixed
    # cohort also contains the seventh-place brand.
    rows = [row for row in result["rankings_by_year"]["2023"] if not row["is_others"]]
    assert [row["brand"] for row in rows] == ["A", "B", "C", "D", "E", "F", "G"]
    assert [row["rank"] for row in rows] == [1, 2, 3, 4, 5, 6, 7]


def test_b02d1_green_gene_f_remains_visible_at_rank_six() -> None:
    # Given: the B02D1 tracker contains the historical sixth-place brand.
    brands = tuple(
        _brand(
            str(rank),
            name,
            f"Company {rank}",
            {"2023-Q1": value, "2024-Q1": value},
        )
        for rank, (name, value) in enumerate(
            (
                ("헴리브라", 700.0),
                ("애드베이트", 600.0),
                ("그린모노", 500.0),
                ("애디노베이트", 400.0),
                ("진타솔로퓨즈", 300.0),
                ("그린진에프", 200.0),
                ("기타경쟁품", 100.0),
            ),
            start=1,
        )
    )

    # When: the B02D1 brand ranking is assembled.
    result = brand_ranking(brands, focus=brands[0])

    # Then: the known F-095 sixth-place row is not folded into 기타.
    green_gene_f = next(
        row
        for row in result["rankings_by_year"]["2023"]
        if row.get("brand") == "그린진에프"
    )
    assert green_gene_f["rank"] == 6
    assert green_gene_f["value"] == 200.0


def test_brand_ranking_keeps_real_zero_visible_and_reconciles_to_market_total() -> None:
    # Given: one selected competitor has a real zero in the historical year.
    brands = (
        _brand("a", "A", "A Co", {"2023-Q1": 100.0, "2024-Q1": 100.0}),
        _brand("b", "B", "B Co", {"2023-Q1": 0.0, "2024-Q1": 90.0}),
        _brand("c", "C", "C Co", {"2023-Q1": 30.0, "2024-Q1": 80.0}),
    )

    # When: the ranking is built for all years.
    result = brand_ranking(brands, focus=brands[0])

    # Then: zero is retained as data and values still reconcile without renormalizing.
    rows = result["rankings_by_year"]["2023"]
    zero = next(row for row in rows if row.get("brand") == "B")
    assert zero["value"] == 0.0
    assert zero["ms_pct"] == 0.0
    assert sum(float(row["value"]) for row in rows) == pytest.approx(130.0)


def _brand(
    key: str,
    name: str,
    company: str,
    history: dict[str, float],
) -> BrandMetric:
    total = sum(history.values())
    latest_period = max(history)
    return BrandMetric(
        brand_key=key,
        brand_name=name,
        atc4_code="B02D1",
        total_value=total,
        market_share_pct=0.0,
        rank=0,
        latest_period=latest_period,
        latest_value=history[latest_period],
        monthly_series=tuple({"period": period, "value": value} for period, value in sorted(history.items())),
        history_by_period=history,
        analysis_row={"by_dimension": {"company": company}},
    )
