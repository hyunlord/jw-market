from __future__ import annotations

import duckdb
import pandas as pd
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.etl.io.mart.general_history import cagr_from_history
from pipeline.etl.io.mart import general_iqvia
from pipeline.etl.io.ubist_loader import UBIST_LOAD_RETENTION_MONTHS
from pipeline.etl.io.mart.general_rows import build_brand_rows, build_market_rows
from pipeline.etl.io.mart.strategic_scope import (
    recompute_market_scoped_metric_history,
)
from pipeline.etl.io.mart.general_ubist import (
    _available_ubist_periods,
    load_ubist_base_frame,
)
from pipeline.etl.io.mart.general_window import (
    calculation_period_scope,
    rolling_period_scope,
)


def _month_labels(start_year: int, start_month: int, count: int) -> tuple[str, ...]:
    return tuple(
        f"{start_year + (start_month - 1 + offset) // 12:04d}-"
        f"{(start_month - 1 + offset) % 12 + 1:02d}"
        for offset in range(count)
    )


def _quarter_labels(start_year: int, start_quarter: int, count: int) -> tuple[str, ...]:
    return tuple(
        f"{start_year + (start_quarter - 1 + offset) // 4:04d}-Q"
        f"{(start_quarter - 1 + offset) % 4 + 1}"
        for offset in range(count)
    )


def test_ubist_rolling_scope_keeps_latest_60_of_65_months() -> None:
    periods = _month_labels(2021, 1, 65)

    selected = rolling_period_scope(periods, source="ubist")

    assert selected == periods[-60:]
    assert selected[0] == "2021-06"
    assert selected[-1] == "2026-05"


def test_ubist_calculation_scope_keeps_exact_five_year_baseline() -> None:
    periods = _month_labels(2021, 6, 61)

    calculation = calculation_period_scope(periods, source="ubist")
    displayed = rolling_period_scope(periods, source="ubist")

    assert calculation == periods
    assert calculation[0] == "2021-06"
    assert calculation[-1] == "2026-06"
    assert displayed == periods[-60:]
    assert displayed[0] == "2021-07"
    assert displayed[-1] == "2026-06"


def test_iqvia_window_contract_separates_retention_calculation_and_display() -> None:
    periods = _quarter_labels(2020, 1, 25)

    assert rolling_period_scope(periods, source="iqvia_nsa", purpose="retention") == periods[-24:]
    assert rolling_period_scope(periods, source="iqvia_nsa", purpose="calculation") == periods[-21:]
    assert rolling_period_scope(periods, source="iqvia_nsa", purpose="display") == periods[-20:]


def test_iqvia_cache_reader_receives_latest_21_quarter_calculation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    periods = _quarter_labels(2020, 1, 25)
    observed: dict[str, tuple[str, ...]] = {}
    storage = object()

    monkeypatch.setattr(general_iqvia, "_iqvia_cache_configured", lambda: True)
    monkeypatch.setattr(general_iqvia, "_approved_cache_source_sha256", lambda: "a" * 64)
    monkeypatch.setattr(
        general_iqvia,
        "build_iqvia_minio_cache_storage",
        lambda: storage,
    )
    monkeypatch.setattr(
        general_iqvia,
        "available_iqvia_cache_quarters_for_source_sha256",
        lambda _sha, _storage: periods,
    )

    def iter_records(_sha, _storage, *, quarters, **_kwargs):
        observed["quarters"] = quarters
        return iter(())

    monkeypatch.setattr(
        general_iqvia,
        "iter_iqvia_parquet_cache_for_source_sha256",
        iter_records,
    )

    frame = general_iqvia.load_iqvia_base_frame()

    assert frame.empty
    assert observed["quarters"] == periods[-21:]


def test_iqvia_brand_payload_does_not_expose_calculation_only_quarter() -> None:
    periods = _quarter_labels(2021, 1, 21)
    source_rows = [
        {
            "brand_key": "livalo",
            "brand_name": "리바로",
            "product_name": "리바로정",
            "product_code": "LIVALO TAB",
            "atc4_code": "C10A1",
            "atc4_desc": "Statins",
            "period_yyyymm": period,
            "raw_value": float(index + 1),
            "raw_sales": float(index + 1),
            "audit_code": "KPA",
            "channel": "KPA",
            "specialty": None,
            "manufacturer": "JW",
            "company": "JW",
            "payload_static": {"MFR NAME KOR": "JW"},
        }
        for index, period in enumerate(periods)
    ]

    row = build_brand_rows(
        "iqvia_nsa", "sales", pd.DataFrame(source_rows), {}
    )[0]

    assert tuple(row["raw_value_history"]) == periods[-20:]
    assert tuple(row["metric_history"]) == periods[-20:]
    assert tuple(row["extended_metric_history"]) == periods[-20:]
    assert row["payload"]["period_count"] == 20


@pytest.mark.parametrize(
    ("source", "periods"),
    [
        ("ubist", _month_labels(2025, 1, 12)),
        ("iqvia_nsa", _quarter_labels(2025, 1, 6)),
    ],
)
def test_rolling_scope_keeps_all_periods_when_history_is_shorter_than_window(
    source: str,
    periods: tuple[str, ...],
) -> None:
    assert rolling_period_scope(periods, source=source) == periods


def test_rolling_scope_drops_oldest_period_when_new_period_arrives() -> None:
    initial = _month_labels(2021, 1, 60)
    advanced = (*initial, "2026-01")

    assert rolling_period_scope(initial, source="ubist")[0] == "2021-01"
    assert rolling_period_scope(advanced, source="ubist")[0] == "2021-02"
    assert rolling_period_scope(advanced, source="ubist")[-1] == "2026-01"


def test_raw_ubist_loader_keeps_latest_61_calculation_months(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    periods = _month_labels(2021, 1, 65)
    source_row = {
        "약품코드": "p1",
        "제품": "Product One",
        "브랜드": "Brand One",
        "ATC": "C10A1 Test",
        "종별": "의원",
        "진료과": "가정의학과",
        "제조사": "Maker",
        "판매사": "Seller",
        "성분": "A",
        "성분용량": "10mg",
        "제형": "정제",
        "투여경로": "경구",
        "급여구분": "급여",
        "rx_amt": 10.0,
        "rx_qty": 2.0,
    }
    for period in periods:
        raw_dir = (
            tmp_path
            / "ubist"
            / f"year={period[:4]}"
            / f"month={period[5:]}"
        )
        raw_dir.mkdir(parents=True)
        frame = pd.DataFrame([{**source_row, "period_yyyymm": period.replace("-", "")}])
        with duckdb.connect() as connection:
            connection.register("source", frame)
            connection.execute(
                f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)"
            )

    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    frame = load_ubist_base_frame()
    selected = tuple(
        sorted(
            {
                f"{str(period)[:4]}-{str(period)[4:]}"
                for period in frame["period_yyyymm"]
            }
        )
    )

    assert selected == periods[-61:]


def test_enriched_ubist_period_scope_uses_global_source_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    periods = _month_labels(2021, 1, 65)
    enriched_root = tmp_path / "enriched"
    for ml_id, selected_periods in (
        ("ml_complete", periods),
        ("ml_sparse", (*periods[:5], *periods[-55:])),
    ):
        output = enriched_root / f"ml_id={ml_id}"
        output.mkdir(parents=True)
        frame = pd.DataFrame(
            {
                "source": ["ubist"] * len(selected_periods),
                "period_yyyymm": [
                    period.replace("-", "") for period in selected_periods
                ],
            }
        )
        with duckdb.connect() as connection:
            connection.register("source", frame)
            connection.execute(
                f"COPY source TO '{output / 'data.parquet'}' (FORMAT PARQUET)"
            )

    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "missing-raw"))
    monkeypatch.setenv("S4_ENRICHED_DIR", str(enriched_root))

    assert _available_ubist_periods() == periods[-61:]


def test_enriched_ubist_scope_uses_its_own_latest_period_when_raw_is_newer(
    tmp_path,
) -> None:
    enriched_periods = _month_labels(2020, 12, 65)
    output = tmp_path / "enriched.parquet"
    frame = pd.DataFrame(
        {
            "source": ["ubist"] * len(enriched_periods),
            "period_yyyymm": [
                period.replace("-", "") for period in enriched_periods
            ],
        }
    )
    with duckdb.connect() as connection:
        connection.register("source", frame)
        connection.execute(f"COPY source TO '{output}' (FORMAT PARQUET)")

    selected = _available_ubist_periods(enriched_pattern=str(output))

    assert selected == enriched_periods[-61:]
    assert selected[-1] == "2026-04"


def test_five_year_cagr_uses_exact_60_month_baseline() -> None:
    periods = _month_labels(2021, 6, 61)
    history = {period: 100.0 + index for index, period in enumerate(periods)}
    expected = (160.0 / 100.0) ** (1 / 5) - 1

    assert cagr_from_history(history, periods[-1], 5) == pytest.approx(expected)


def test_five_year_cagr_is_not_calculable_without_exact_baseline() -> None:
    periods = _month_labels(2021, 7, 60)
    history = {period: 100.0 + index for index, period in enumerate(periods)}

    assert cagr_from_history(history, periods[-1], 5) is None


def test_public_market_series_does_not_leak_calculation_baseline() -> None:
    periods = _month_labels(2021, 6, 61)
    displayed = periods[-60:]
    frame = pd.DataFrame(
        [
            {
                "brand_key": "brandone",
                "brand_name": "Brand One",
                "product_name": "Product One",
                "product_code": "p1",
                "atc4_code": "C10A1",
                "atc4_desc": "C10A1 Test",
                "period_yyyymm": period,
                "raw_value": 100.0 + index,
                "audit_code": "p1",
                "channel": "CLINIC",
                "specialty": "CARDIO",
                "manufacturer": "Maker",
                "company": "Seller",
            }
            for index, period in enumerate(periods)
        ]
    )

    brand_rows = build_brand_rows("ubist", "sales", frame, {})
    market_rows = build_market_rows("ubist", "sales", brand_rows)

    assert tuple(brand_rows[0]["raw_value_history"]) == periods
    assert tuple(brand_rows[0]["metric_history"]) == displayed
    assert tuple(brand_rows[0]["extended_metric_history"]) == displayed
    assert tuple(market_rows[0]["market_size_series"]) == displayed
    assert periods[0] not in market_rows[0]["hhi_series"]
    assert brand_rows[0]["extended_metric_history"][periods[-1]][
        "cagr_5y"
    ] == pytest.approx((160.0 / 100.0) ** (1 / 5) - 1)


def test_strategic_series_does_not_leak_calculation_baseline() -> None:
    periods = _month_labels(2021, 6, 61)
    displayed = periods[-60:]
    rows = [
        {
            "brand_key": "brandone",
            "brand_name": "Brand One",
            "source": "ubist",
            "measure": "sales",
            "raw_value_history": {
                period: 100.0 + index for index, period in enumerate(periods)
            },
        }
    ]

    recompute_market_scoped_metric_history(rows)

    assert tuple(rows[0]["raw_value_history"]) == periods
    assert tuple(rows[0]["metric_history"]) == displayed
    assert tuple(rows[0]["extended_metric_history"]) == displayed
    assert rows[0]["extended_metric_history"][periods[-1]]["cagr_5y"] == pytest.approx(
        (160.0 / 100.0) ** (1 / 5) - 1
    )


def test_ubist_load_retention_remains_72_months() -> None:
    assert UBIST_LOAD_RETENTION_MONTHS == 72


def test_five_year_cqgr_is_absent_inside_20_quarter_display_window() -> None:
    periods = _quarter_labels(2021, 2, 20)
    history = {period: 100.0 + index for index, period in enumerate(periods)}

    assert cagr_from_history(history, periods[-1], 5) is None


def test_five_year_cqgr_uses_exact_baseline_from_21_quarters() -> None:
    periods = _quarter_labels(2021, 1, 21)
    history = {period: 100.0 + index for index, period in enumerate(periods)}
    expected = (120.0 / 100.0) ** (1 / 5) - 1

    assert cagr_from_history(history, periods[-1], 5) == pytest.approx(expected)
