from __future__ import annotations

from pipeline.scripts.api.brand_activity_csd_activity_contract import CsdActivitySeriesRequest
from pipeline.scripts.api.models.brand_activity import (
    BrandActivityInterestRxRequest,
    BrandActivityTopicsRequest,
    CsdTimeseriesRequest,
)
from pipeline.scripts.gates.latency_matrix_cases import build_latency_matrix_cases
from pipeline.scripts.gates.release_acceptance import check_latency_matrix_evidence
from pipeline.scripts.gates.latency_matrix_required import REQUIRED_GROUP_SCOPES


DEFAULT_BRANDS = [
    {
        "brand": "리바로",
        "atc_codes": ["C10A1"],
        "general_sources": ["UBIST", "IQVIA"],
        "strategic_sources": ["UBIST"],
    }
]

SEARCH_PAYLOADS = {
    "리바로": [
        {
            "brand": "리바로",
            "general_sources": ["UBIST", "IQVIA"],
            "strategic_sources": ["UBIST"],
            "contexts": [
                {"view_kind": "general", "market_id": "C10A1", "has_market_data": True},
            ],
        }
    ]
}


def test_latency_matrix_includes_required_general_scope_scenarios() -> None:
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        SEARCH_PAYLOADS,
        requested_brands=("리바로",),
    )
    by_id = {case.identifier: case for case in cases}

    for count in (1, 2, 5, 10):
        identifier = f"required_general:atc4_{count}:ubist:sales"
        assert identifier in by_id
        assert len(by_id[identifier].body["filters"]["atc4"]) == count

    assert by_id["required_general:atc4_1:ubist:volume"].body["measure"] == "volume"
    assert by_id["required_general:atc4_1:iqvia:sales"].body["measure"] == "sales"
    assert by_id["required_general:atc4_1:iqvia:unit"].body["measure"] == "unit"


def test_latency_matrix_includes_class_and_molecule_narrowing() -> None:
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        SEARCH_PAYLOADS,
        requested_brands=("리바로",),
    )
    by_id = {case.identifier: case for case in cases}

    class_case = by_id["required_general:iqvia:molecule_type:single:sales"]
    molecule_case = by_id["required_general:iqvia:molecule_desc:pitavastatin:sales"]

    assert class_case.body["filters"]["analysis_level"]["iqvia"] == {
        "molecule_type": ["SINGLE"]
    }
    assert molecule_case.body["filters"]["analysis_level"]["iqvia"] == {
        "molecule_desc": ["PITAVASTATIN"]
    }


def test_required_group_scope_bodies_match_public_request_contracts() -> None:
    search_payloads = {
        **SEARCH_PAYLOADS,
        "가드렛": [
            {
                "brand": "가드렛",
                "general_sources": ["UBIST"],
                "strategic_sources": ["UBIST"],
                "contexts": [
                    {"view_kind": "general", "market_id": "A10N3", "has_market_data": True},
                ],
            }
        ],
    }
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        search_payloads,
        requested_brands=("리바로", "가드렛"),
        group_scopes=REQUIRED_GROUP_SCOPES,
    )
    by_id = {case.identifier: case for case in cases}

    validators = {
        "topics": BrandActivityTopicsRequest,
        "csd_timeseries": CsdTimeseriesRequest,
        "csd_activity": CsdActivitySeriesRequest,
        "interest_rx": BrandActivityInterestRxRequest,
    }
    for option_id, member in REQUIRED_GROUP_SCOPES:
        for surface, request_model in validators.items():
            case = by_id[f"brand_activity_group:{surface}:{option_id}:{member}"]
            parsed = request_model.model_validate(case.body)

            assert parsed.filters.market_scope.option_id == option_id
            assert parsed.filters.market_scope.member == member


def test_latency_matrix_gate_rejects_missing_required_surface_contract() -> None:
    evidence = {
        "classification": "census",
        "provenance": {
            "population_rule": "default brands plus required edge brands; all discovered contexts and listed sources",
            "reference": "live-production",
        },
        "requested_brands": ["리바로"],
        "resolved_brands": ["리바로"],
        "required_cd_brands": ["악템라", "가드렛"],
        "required_group_scopes": [
            ["group:livalo_family", "리바로"],
            ["group:gardlet_family", "가드렛"],
        ],
        "expected_cases": ["brands"],
        "observations": [
            {"id": "brands", "candidate_status": 200, "reference_status": 200, "parity": True}
        ],
    }

    result = check_latency_matrix_evidence(evidence, "failure-injection")

    assert result.exit_code == 1
    assert any("required latency cases missing" in detail for detail in result.details)
