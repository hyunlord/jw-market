from __future__ import annotations

from pipeline.etl.io.mart.strategic_ubist_channels import build_ubist_channel_totals
from pipeline.scripts.etl import ubist_channel_resolver
from pipeline.scripts.utils.ubist_channel_mapping import parse_channel_code


def test_build_ubist_channel_totals_when_general_matrix_has_raw_pairs() -> None:
    matrix = {
        "상급종합병원": {
            "순환기(Cardiology IM)": {"2026-04": 10.0},
            "내분비(Endocrinology IM)": {"2026-04": 7.0},
        },
        "종합병원": {
            "순환기(Cardiology IM)": {"2026-04": 15.0},
            "분리되지 않은 내과": {"2026-04": 9.0},
        },
        "의원": {
            "가정의학과(FM)": {"2026-04": 3.0},
            "일반의(GP)": {"2026-04": 4.0},
            "분리되지 않은 내과": {"2026-04": 5.0},
        },
    }

    result = build_ubist_channel_totals(matrix)

    assert result["by_display"]["종합병원 순환기"]["2026-04"] == 25.0
    assert result["by_code"]["GH Cardio"]["2026-04"] == 25.0
    assert result["by_display"]["종합병원 내분비"]["2026-04"] == 7.0
    assert result["by_code"]["GH IGF"]["2026-04"] == 9.0
    assert result["by_display"]["의원 IGF"]["2026-04"] == 12.0


def test_resolver_uses_strategic_channel_totals_context_when_available() -> None:
    rows = [
        {
            "brand_name": "리바로",
            "ubist_channel_by_display": {
                "종합병원 순환기": {"2026-04": 25.0},
                "의원 IGF": {"2026-04": 12.0},
            },
            "ubist_channel_by_code": {
                "GH Cardio": {"2026-04": 25.0},
                "CL IGF": {"2026-04": 12.0},
            },
        }
    ]

    with ubist_channel_resolver.strategic_channel_totals_context(rows):
        result = ubist_channel_resolver.resolve_market_channels(
            rows=rows,
            market={"target_ubist_1": "GH Cardio", "target_ubist_2": "CL IGF"},
            measure="sales",
        )

    assert result["specialty_channels"] == ["전체", "주요고객 종합병원 순환기", "의원 IGF"]
    assert result["target_channels"][0]["code"] == "TGH Cardio"
    assert result["target_channels"][0]["facility_raw_values"] == ["상급종합병원", "종합병원"]
    assert rows[0]["__ubist_dual_channel_data"]["주요고객 종합병원 순환기"]["2026-04"] == 25.0
    assert rows[0]["__ubist_specialty_channel_data"]["의원 IGF"]["2026-04"] == 12.0

    general_channel = parse_channel_code("GH Cardio")
    assert general_channel is not None
    assert general_channel.display_name == "종합병원 순환기"
    assert general_channel.facility_raw_values == ("상급종합병원", "종합병원", "병원")
