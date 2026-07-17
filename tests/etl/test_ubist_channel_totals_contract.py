from __future__ import annotations

import json

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


def test_resolver_fills_from_latest_raw_matrix_with_others_and_catalog_exclusion() -> None:
    rows = [
        {
            "brand_name": "시그마트",
            "channel_specialty_matrix": {
                "상급종합병원": {
                    "순환기(Cardiology IM)": {"2026-05": 20.0},
                    "신장(Nephrology IM)": {"2025-01": 10_000.0, "2026-05": 50.0},
                },
                "종합병원": {
                    "순환기(Cardiology IM)": {"2026-05": 30.0},
                    "신경과(NR)": {"2026-05": 40.0},
                },
                "병원": {
                    "Others(병원,보건기관, 그 외 요양기관)": {"2026-05": 80.0},
                },
                "의원": {
                    "분리되지 않은 내과": {"2026-05": 70.0},
                },
            },
            "ubist_channel_by_code": {
                "TGH Nephro": {"2025-01": 10_000.0, "2026-05": 50.0},
                "TGH Cardio": {"2026-05": 50.0},
                "CL IGF": {"2026-05": 70.0},
            },
        },
        {
            "brand_name": "경쟁품",
            "channel_specialty_matrix": {
                "종합병원": {
                    "순환기(Cardiology IM)": {"2026-05": 10.0},
                },
                "병원": {
                    "Others(병원,보건기관, 그 외 요양기관)": {"2026-05": 20.0},
                },
            },
        },
    ]

    with ubist_channel_resolver.strategic_channel_totals_context(rows):
        result = ubist_channel_resolver.resolve_market_channels(
            rows=rows,
            market={"target_ubist_1": "GH Cardio", "target_ubist_2": "CL IGF"},
            measure="sales",
        )

    assert result["specialty_channels"] == [
        "전체",
        "주요고객 종합병원 순환기",
        "의원 IGF",
        "병원",
        "주요고객 종합병원 신장",
    ]
    assert result["fallback_codes"] == ["Semi Others", "TGH Nephro"]
    assert rows[0]["__ubist_specialty_channel_data"]["병원"]["2026-05"] == 80.0
    assert rows[1]["__ubist_specialty_channel_data"]["병원"]["2026-05"] == 20.0


def test_resolver_decodes_each_raw_matrix_once_per_request(monkeypatch) -> None:
    matrices = [
        {
            "종합병원": {
                "순환기(Cardiology IM)": {"2026-04": 10.0, "2026-05": 20.0},
            }
        },
        {
            "의원": {
                "분리되지 않은 내과": {"2026-04": 30.0, "2026-05": 40.0},
            }
        },
    ]
    encoded = [json.dumps(matrix, ensure_ascii=False) for matrix in matrices]
    rows = [
        {"brand_name": f"brand-{index}", "channel_specialty_matrix": raw}
        for index, raw in enumerate(encoded)
    ]
    original_loads = ubist_channel_resolver.json.loads
    decode_calls: list[str] = []

    def counted_loads(raw: str) -> object:
        if raw in encoded:
            decode_calls.append(raw)
        return original_loads(raw)

    monkeypatch.setattr(ubist_channel_resolver.json, "loads", counted_loads)

    result = ubist_channel_resolver.resolve_market_channels(
        rows=rows,
        market={"target_ubist_1": "GH Cardio"},
        measure="sales",
    )

    assert len(decode_calls) == len(rows)
    assert result["specialty_channels"][1] == "주요고객 종합병원 순환기"
    assert all("__channel_specialty_matrix" not in row for row in rows)
