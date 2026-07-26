from __future__ import annotations

import duckdb
import pandas as pd
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.etl.io.mart.general_history import cagr_from_history
from pipeline.etl.io.mart import general_iqvia
from pipeline.etl.io.mart.general_ubist import (
    _available_ubist_periods,
    load_ubist_base_frame,
)
from pipeline.etl.io.mart.general_window import rolling_period_scope


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


def test_iqvia_rolling_scope_keeps_latest_20_quarters() -> None:
    periods = _quarter_labels(2020, 1, 25)

    selected = rolling_period_scope(periods, source="iqvia_nsa")

    assert selected == periods[-20:]


def test_iqvia_cache_reader_receives_default_latest_20_quarter_scope(
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
    assert observed["quarters"] == periods[-20:]


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


def test_raw_ubist_loader_uses_only_latest_60_source_months(
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

    assert selected == periods[-60:]


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

    assert _available_ubist_periods() == periods[-60:]


def test_five_year_cagr_uses_actual_elapsed_span_inside_60_month_window() -> None:
    periods = _month_labels(2021, 6, 60)
    history = {period: 100.0 + index for index, period in enumerate(periods)}
    expected = (159.0 / 100.0) ** (1 / (59 / 12)) - 1

    assert cagr_from_history(history, periods[-1], 5) == pytest.approx(expected)


def test_five_year_cqgr_uses_actual_elapsed_span_inside_20_quarter_window() -> None:
    periods = _quarter_labels(2021, 2, 20)
    history = {period: 100.0 + index for index, period in enumerate(periods)}
    expected = (119.0 / 100.0) ** (1 / (19 / 4)) - 1

    assert cagr_from_history(history, periods[-1], 5) == pytest.approx(expected)
