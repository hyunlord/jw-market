from __future__ import annotations

import pytest

from pipeline.scripts.etl import build_cache_cause as cause
from pipeline.scripts.etl import build_cache_market_status as market_status
from pipeline.scripts.etl import cache_build_common
from pipeline.scripts.api.composers.number_format import deep_format_numbers


PERIODS = ["2021-06"]


def _dimension_row(
    *,
    specialty: dict[str, object] | None,
    channel: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "brand_name": "표본",
        "brand_key": "표본",
        "by_dimension": {"molecule": "Esomeprazole"},
        "metric_history": {"2021-06": {"raw_value": 999.0}},
        "dimension_specialty_data": {"molecule": specialty or {}},
        "dimension_channel_data": {"molecule": channel or {}},
    }


def test_ubist_dimension_channel_coalesces_partial_labels_without_940_overcount_regression() -> None:
    row = _dimension_row(
        specialty={
            "Esomeprazole": {"상급종병": {"2021-06": {"raw_value": 2_371_917_674.13}}},
        },
        channel={
            "Esomeprazole": {
                "상급종합병원": {"2021-06": {"raw_value": 2_371_917_674.13}},
            },
            "Missing": {
                "상급종합병원": {"2021-06": {"raw_value": 1_086_265_816.53}},
            },
        },
    )

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="Molecule",
        periods=PERIODS,
        source="UBIST",
        channel="상급종병",
        target_name=None,
        top_n=None,
    )
    values = {segment["name"]: segment["value_series"][0] for segment in segments}

    assert values["Esomeprazole"] == pytest.approx(2_371_917_674.13)
    assert values["Missing"] == pytest.approx(1_086_265_816.53)
    assert sum(values.values()) == pytest.approx(3_458_183_490.66)


@pytest.mark.parametrize(
    ("specialty", "channel", "expected"),
    [
        (
            {"Esomeprazole": {"상급종병": {"2021-06": {"raw_value": 10.0}}}},
            {"Esomeprazole": {"상급종합병원": {"2021-06": {"raw_value": 10.0}}}},
            10.0,
        ),
        (
            {},
            {"Esomeprazole": {"상급종합병원": {"2021-06": {"raw_value": 7.0}}}},
            7.0,
        ),
    ],
)
def test_ubist_dimension_channel_complete_and_legacy_branches(
    specialty: dict[str, object],
    channel: dict[str, object],
    expected: float,
) -> None:
    segments = cause._segment_rows_for_level(
        rows=[_dimension_row(specialty=specialty, channel=channel)],
        level="Molecule",
        periods=PERIODS,
        source="UBIST",
        channel="상급종병",
        target_name=None,
        top_n=None,
    )

    assert segments[0]["value_series"] == [expected]


def test_rows_for_dimension_uses_channel_fallback_for_missing_specialty_label() -> None:
    row = _dimension_row(
        specialty={"Other": {"상급종병": {"2021-06": {"raw_value": 10.0}}}},
        channel={
            "Esomeprazole": {
                "상급종합병원": {"2021-06": {"raw_value": 1_086_265_816.53}},
            }
        },
    )

    selected = cause._rows_for_dimension(
        [row],
        "Molecule",
        "Esomeprazole",
        PERIODS,
        source="UBIST",
        channel="상급종병",
    )

    assert cause._metric_history(selected[0])["2021-06"]["raw_value"] == pytest.approx(1_086_265_816.53)


def test_level_top5_total_is_full_market_when_dimension_has_no_usable_series() -> None:
    rows = [
        {
            "brand_name": "엔커버",
            "brand_key": "엔커버",
            "source": "iqvia_nsa",
            "metric_history": {"2026-Q1": {"raw_value": 16_386_730_542.0}},
            "by_dimension": {"strength": "100mg"},
            "dimension_data": {"strength": {}},
        }
    ]
    levels = {
        "levels": ["용량"],
        "periods_monthly": [],
        "periods_quarterly": ["2026-Q1"],
        "data": {"용량": {"segments": [], "by_channel": {"전체": []}}},
    }

    trend = cause._level_top5_trend(levels, rows, "IQVIA", "엔커버")

    assert trend["by_level"]["용량"]["total_market_value"] == 16_386_730_542.0


@pytest.mark.parametrize(
    "expected",
    [
        99_846.0,
        5_497_709_295.0,
        1_622_715_700.0,
        6_781_815.0,
        16_386_730_542.0,
        3_766_367_920.0,
        4_751_931.0,
        13_602_070_032.0,
        9_569_082.0,
        8_876_967.0,
        119_261_412_238.0,
        456_443.0,
        1_627_490_246.0,
        1_850_568.0,
        53_315_910_038.0,
        2_032_802_500.0,
        2_538_688.0,
        10_484_394_920.0,
    ],
)
def test_iqvia_empty_dimension_uses_full_market_total(expected: float) -> None:
    rows = [
        {
            "brand_name": "표본",
            "brand_key": "표본",
            "source": "iqvia_nsa",
            "metric_history": {"2026-Q1": {"raw_value": expected}},
            "by_dimension": {"strength": "unknown"},
            "dimension_data": {"strength": {}},
        }
    ]
    levels = {
        "levels": ["용량"],
        "periods_monthly": [],
        "periods_quarterly": ["2026-Q1"],
        "data": {"용량": {"segments": [], "by_channel": {"전체": []}}},
    }

    trend = cause._level_top5_trend(levels, rows, "IQVIA", "표본")

    assert trend["by_level"]["용량"]["total_market_value"] == expected


def test_iqvia_nonempty_dimension_options_are_unchanged() -> None:
    row = {
        "brand_name": "표본",
        "brand_key": "표본",
        "source": "iqvia_nsa",
        "metric_history": {"2026-Q1": {"raw_value": 20.0}},
        "by_dimension": {"strength_pack": "100mg"},
        "dimension_data": {"strength_pack": {"100mg": {"2026-Q1": {"raw_value": 20.0}}}},
    }

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="용량",
        periods=["2026-Q1"],
        source="IQVIA",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    assert segments == [{
        "name": "100mg",
        "rank": 1,
        "recent_share_pct": 100.0,
        "series_pct": [100.0],
        "value_series": [20.0],
    }]


def test_active_membership_excludes_rows_and_counts_distinct_names() -> None:
    rows = [
            {"ml_id": "ml_006", "cd_id": "cd_006", "canonical_name": "A", "is_excluded": 0},
            {"ml_id": "ml_006", "cd_id": "cd_006", "canonical_name": "A", "is_excluded": 0},
            {"ml_id": "ml_006", "cd_id": "cd_006", "canonical_name": "B", "is_excluded": 1},
    ]

    assert [row["name"] for row in cause._catalog_members_for_market(rows, "ml_006")] == ["A"]
    assert market_status._market_brand_count(rows, "ml_006") == 1
    assert market_status._direct_competition_count(rows, "cd_006") == 1


def test_catalog_required_columns_reject_stale_snapshot() -> None:
    class Snapshot:
        columns = ["ml_id"]

    with pytest.raises(RuntimeError, match="atc_codes_json"):
        cache_build_common.validate_catalog_schema("ml_market", Snapshot())


def test_market_status_applies_round_down_only_at_api_boundary() -> None:
    payload = market_status.source_card_payload(
        {
            "source": "ubist",
            "measure": "sales",
            "metric_history": {"2026-05": {"raw_value": 1.23456789}},
        },
        market_recent=3.0,
    )

    assert payload["ms_recent_pct"] == pytest.approx(41.152263)
    assert deep_format_numbers(payload)["ms_recent_pct"] == 41.1522


def test_catalog_manifest_records_source_provenance() -> None:
    manifest = cache_build_common.decode_json(cache_build_common.catalog_input_manifest({
        "ml_market": [{
            "source_file_version": "mi-master-v1",
            "catalog_manifest_hash": "a" * 64,
            "ingested_at": "2026-07-13 00:00:00",
        }],
    }))

    assert manifest["inputs"]["ml_market"]["row_count"] == 1
    assert manifest["inputs"]["ml_market"]["catalog_manifest_hashes"] == ["a" * 64]
    assert len(manifest["manifest_sha256"]) == 64


def test_market_status_builder_uses_slim_safe_company_mapping() -> None:
    assert market_status.MARKET_STATUS_COMPANY_BY_BRAND["리바로"] == "일동제약"
    assert len(market_status.MARKET_STATUS_COMPANY_BY_BRAND) == 25
