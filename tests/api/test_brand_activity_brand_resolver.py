from __future__ import annotations

from pipeline.scripts.api.brand_activity_brand_filters import applied_brand_filter
from pipeline.scripts.api.brand_activity_brand_resolver import BrandCandidate, _select_choices
from pipeline.scripts.api.brand_activity_channel_axis import channel_axis_sales_value, parse_ubist_channel_axis
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta


def test_brand_filter_uses_or_within_dimension_and_and_across_dimensions() -> None:
    candidates = (
        _candidate("선택", rank=99, sales=1.0, dimensions={"atc4": ("C10A1",), "molecule": ("other",), "class": ("Z",)}),
        _candidate("A", rank=1, sales=100.0, dimensions={"atc4": ("C10A1",), "molecule": ("pitavastatin",), "class": ("스타틴",)}),
        _candidate("B", rank=2, sales=90.0, dimensions={"atc4": ("C10A1",), "molecule": ("pitavastatin",), "class": ("복합제",)}),
        _candidate("C", rank=3, sales=80.0, dimensions={"atc4": ("C10A1",), "molecule": ("ezetimibe",), "class": ("스타틴",)}),
        _candidate("D", rank=4, sales=70.0, dimensions={"atc4": ("C10C0",), "molecule": ("pitavastatin",), "class": ("스타틴",)}),
    )
    applied = {"atc4": ["C10A1"], "molecule": ["pitavastatin"], "class": ["스타틴", "복합제"]}

    choices = _select_choices(candidates, selected_brand="선택", applied_filter=applied)

    assert [choice.brand_key for choice in choices] == ["선택", "A", "B"]
    assert choices[0].is_selected is True


def test_general_default_filter_applies_market_atc4() -> None:
    assert applied_brand_filter("general", "c10a1", {}) == {"atc4": ["C10A1"]}


def test_applied_filter_echoes_ubist_channel_axis_without_breaking_dimension_filters() -> None:
    payload = {"channel_axis": {"ubist": {"facility": ["의원"], "specialty": ["순환기(Cardiology IM)"]}}}

    applied = applied_brand_filter("general", "C10A1", payload)

    assert applied["atc4"] == ["C10A1"]
    assert applied["channel_axis"] == {
        "source": "ubist",
        "facility": ["의원"],
        "specialty": ["순환기(Cardiology IM)"],
        "pairs": [],
    }


def test_channel_axis_sales_value_uses_raw_ubist_matrix_slice() -> None:
    channel_axis = parse_ubist_channel_axis(
        {"channel_axis": {"ubist": {"facility": ["종합병원"], "specialty": ["순환기(Cardiology IM)"]}}}
    )
    row = {
        "channel_specialty_matrix": {
            "종합병원": {"순환기(Cardiology IM)": {"2026-04": 10, "2026-05": 20}},
            "의원": {"순환기(Cardiology IM)": {"2026-05": 100}},
        }
    }

    assert channel_axis_sales_value(row, channel_axis, "2026-Q2") == 30.0


def _candidate(brand_key: str, *, rank: int, sales: float, dimensions: dict[str, tuple[str, ...]]) -> BrandCandidate:
    return BrandCandidate(
        meta=BrandMeta(brand_key=brand_key, brand_name=brand_key, product_codes=(brand_key.upper(),), is_jw=False),
        dimensions=dimensions,
        sales_rank=rank,
        sales_value=sales,
    )
