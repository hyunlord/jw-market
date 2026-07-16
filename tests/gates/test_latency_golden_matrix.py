from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from pipeline.scripts.gates.latency_matrix_cases import build_latency_matrix_cases
from pipeline.scripts.gates.latency_matrix_runtime import RawResponse, collect_latency_matrix_evidence
from pipeline.scripts.gates.release_acceptance import check_latency_matrix_evidence


DEFAULT_BRANDS = [
    {
        "brand": "리바로",
        "atc_codes": ["C10A1"],
        "general_sources": ["UBIST"],
        "strategic_sources": ["UBIST"],
    },
    {
        "brand": "악템라",
        "atc_codes": ["L04A0"],
        "general_sources": ["UBIST"],
        "strategic_sources": ["UBIST"],
    },
    {
        "brand": "가드렛",
        "atc_codes": ["A10N1"],
        "general_sources": ["UBIST"],
        "strategic_sources": ["UBIST"],
    },
]

SEARCH_PAYLOADS = {
    "리바로": [
        {
            "brand": "리바로",
            "general_sources": ["UBIST"],
            "strategic_sources": ["UBIST"],
            "contexts": [
                {"view_kind": "general", "market_id": "C10A1", "has_market_data": True},
                {"view_kind": "strategic_ml", "market_id": "ml_006", "has_market_data": True},
                {"view_kind": "strategic_cd", "market_id": "cd_006", "has_market_data": True},
            ],
        }
    ],
    "악템라": [
        {
            "brand": "악템라",
            "contexts": [
                {"view_kind": "strategic_cd", "market_id": "cd_008", "has_market_data": True}
            ],
            "strategic_sources": ["UBIST"],
        }
    ],
    "가드렛": [
        {
            "brand": "가드렛",
            "contexts": [
                {"view_kind": "general", "market_id": "A10N1", "has_market_data": True},
                {"view_kind": "strategic_cd", "market_id": "cd_003", "has_market_data": True},
            ],
            "general_sources": ["UBIST"],
            "strategic_sources": ["UBIST"],
        }
    ],
}


def _passing_observation(identifier: str) -> dict[str, object]:
    observation: dict[str, object] = {
        "id": identifier,
        "candidate_status": 200,
        "reference_status": 200,
        "parity": True,
    }
    if identifier.startswith("brand_activity_group:topics:"):
        observation["candidate_populated_brands"] = 1
        observation["reference_populated_brands"] = 1
    return observation


def test_latency_matrix_cases_cover_every_backend_surface() -> None:
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        SEARCH_PAYLOADS,
        requested_brands=("리바로", "악템라", "가드렛"),
        required_cd_brands=("악템라", "가드렛"),
        group_scopes=(("group:livalo_family", "리바로"), ("group:gardlet_family", "가드렛")),
    )
    identifiers = {case.identifier for case in cases}

    assert len(identifiers) == len(cases)
    assert "dynamic:리바로:general:ubist:sales" in identifiers
    assert "dynamic:리바로:strategic_ml:ubist:sales" in identifiers
    assert "dynamic:리바로:strategic_cd:ubist:sales" in identifiers
    assert "deep:리바로:general:C10A1:ubist" in identifiers
    assert "deep:리바로:strategic_ml:ml_006:ubist" in identifiers
    assert "deep:리바로:strategic_cd:cd_006:ubist" in identifiers
    assert "cause:리바로:market_landscape:ml_006:ubist:sales" in identifiers
    assert "cause:리바로:competitive_dynamics:cd_006:ubist:sales" in identifiers
    assert "brand_activity:presence:리바로" in identifiers
    assert "brand_activity:topics:리바로:general:C10A1" in identifiers
    assert "brand_activity:topics:리바로:strategic_ml:ml_006" in identifiers
    assert "brand_activity:topics:리바로:strategic_cd:cd_006" in identifiers
    assert "brand_activity:csd_timeseries:리바로:general:C10A1" in identifiers
    assert "brand_activity:csd_activity:리바로:strategic_cd" in identifiers
    assert "brand_activity:interest_rx:리바로:strategic_ml:ml_006" in identifiers
    assert "filter_options:리바로:strategic_cd:ubist:sales" in identifiers
    assert "dynamic:악템라:strategic_cd:ubist:sales" in identifiers
    assert "dynamic:가드렛:strategic_cd:ubist:sales" in identifiers
    assert "cause:악템라:competitive_dynamics:cd_008:ubist:sales" in identifiers
    assert "cause:가드렛:competitive_dynamics:cd_003:ubist:sales" in identifiers
    assert "brand_activity_group:topics:group:livalo_family:리바로" in identifiers
    assert "brand_activity_group:topics:group:gardlet_family:가드렛" in identifiers
    assert "brand_activity_group:csd_timeseries:group:livalo_family:리바로" in identifiers
    assert "brand_activity_group:csd_activity:group:gardlet_family:가드렛" in identifiers
    assert "brand_activity_group:interest_rx:group:gardlet_family:가드렛" in identifiers
    assert not any("meeting" in identifier for identifier in identifiers)


def test_latency_matrix_cases_keep_brand_activity_source_independent() -> None:
    payloads = json.loads(json.dumps(SEARCH_PAYLOADS, ensure_ascii=False))
    payloads["리바로"][0]["general_sources"] = ["UBIST", "IQVIA"]
    cases = build_latency_matrix_cases(DEFAULT_BRANDS, payloads, requested_brands=("리바로",))
    identifiers = [case.identifier for case in cases]

    assert "dynamic:리바로:general:ubist:sales" in identifiers
    assert "dynamic:리바로:general:iqvia:sales" in identifiers
    assert identifiers.count("brand_activity:topics:리바로:general:C10A1") == 1


def test_latency_matrix_cases_fail_when_required_cd_brand_has_no_cd_context() -> None:
    payloads = json.loads(json.dumps(SEARCH_PAYLOADS, ensure_ascii=False))
    payloads["악템라"][0]["contexts"] = []

    try:
        build_latency_matrix_cases(
            DEFAULT_BRANDS,
            payloads,
            requested_brands=("악템라", "가드렛"),
            required_cd_brands=("악템라", "가드렛"),
        )
    except ValueError as exc:
        assert "required strategic_cd brands unresolved: 악템라" in str(exc)
    else:
        raise AssertionError("missing required strategic_cd context must fail closed")


def test_latency_matrix_gate_requires_complete_200_parity() -> None:
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        SEARCH_PAYLOADS,
        requested_brands=("리바로", "악템라", "가드렛"),
        required_cd_brands=("악템라", "가드렛"),
        group_scopes=(("group:livalo_family", "리바로"), ("group:gardlet_family", "가드렛")),
    )
    expected_cases = [
        "brands",
        "market_status",
        "brand_search:리바로",
        "brand_search:악템라",
        "brand_search:가드렛",
        *(case.identifier for case in cases),
    ]
    evidence = {
        "classification": "census",
        "provenance": {
            "population_rule": "default brands plus required edge brands; all discovered contexts and listed sources",
            "reference": "live-production",
        },
        "requested_brands": ["리바로", "악템라", "가드렛"],
        "resolved_brands": ["리바로", "악템라", "가드렛"],
        "required_cd_brands": ["악템라", "가드렛"],
        "required_group_scopes": [
            ["group:livalo_family", "리바로"],
            ["group:gardlet_family", "가드렛"],
        ],
        "expected_cases": expected_cases,
        "observations": [_passing_observation(identifier) for identifier in expected_cases],
    }

    result = check_latency_matrix_evidence(evidence, "test2")

    assert result.exit_code == 0
    assert result.classification == "census"
    assert result.checked == result.population == len(expected_cases)


def test_latency_matrix_runtime_uses_reference_population_and_masks_only_deep_timestamp() -> None:
    def requester(base_url, case, _timeout_seconds):
        if case.identifier == "brands":
            payload = DEFAULT_BRANDS
        elif case.identifier.startswith("brand_search:"):
            brand = parse_qs(urlsplit(case.path).query)["q"][0]
            payload = SEARCH_PAYLOADS[brand]
        elif case.identifier.startswith("deep:"):
            payload = {"value": case.identifier, "generated_at": base_url}
        elif case.identifier.startswith("brand_activity_group:topics:"):
            payload = {
                "data": {
                    "brands": [
                        {
                            "brand_name": "리바로",
                            "event_count": 12,
                            "topic_shares": [{"topic_id": "T01", "share_pct": 75.0}],
                        }
                    ]
                }
            }
        else:
            payload = {"value": case.identifier}
        return RawResponse(
            status=200,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        )

    evidence = collect_latency_matrix_evidence(
        "http://candidate",
        "http://reference",
        timeout_seconds=1.0,
        max_workers=2,
        requester=requester,
        edge_brands=(),
    )
    result = check_latency_matrix_evidence(evidence, "unit")

    assert evidence["required_cd_brands"] == ["악템라", "가드렛"]
    assert evidence["required_group_scopes"] == [
        ["group:livalo_family", "리바로"],
        ["group:gardlet_family", "가드렛"],
    ]
    assert result.exit_code == 0
    assert result.checked == result.population == len(evidence["expected_cases"])


def test_latency_matrix_gate_rejects_empty_required_group_topics() -> None:
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        SEARCH_PAYLOADS,
        requested_brands=("리바로", "악템라", "가드렛"),
        required_cd_brands=("악템라", "가드렛"),
        group_scopes=(("group:livalo_family", "리바로"), ("group:gardlet_family", "가드렛")),
    )
    expected_cases = [
        "brands",
        "market_status",
        "brand_search:리바로",
        "brand_search:악템라",
        "brand_search:가드렛",
        *(case.identifier for case in cases),
    ]
    observations = [_passing_observation(identifier) for identifier in expected_cases]
    empty_group = next(
        item for item in observations if item["id"] == "brand_activity_group:topics:group:livalo_family:리바로"
    )
    empty_group["candidate_populated_brands"] = 0
    empty_group["reference_populated_brands"] = 0
    evidence = {
        "classification": "census",
        "provenance": {
            "population_rule": "default brands plus required edge brands; all discovered contexts and listed sources",
            "reference": "live-production",
        },
        "requested_brands": ["리바로", "악템라", "가드렛"],
        "resolved_brands": ["리바로", "악템라", "가드렛"],
        "required_cd_brands": ["악템라", "가드렛"],
        "required_group_scopes": [
            ["group:livalo_family", "리바로"],
            ["group:gardlet_family", "가드렛"],
        ],
        "expected_cases": expected_cases,
        "observations": observations,
    }

    result = check_latency_matrix_evidence(evidence, "failure-injection")

    assert result.exit_code == 1
    assert any("required group topics are empty" in detail for detail in result.details)


def test_latency_matrix_gate_failure_injection_exits_one() -> None:
    evidence = {
        "classification": "census",
        "provenance": {
            "population_rule": "default brands plus required edge brands; all discovered contexts and listed sources",
            "reference": "live-production",
        },
        "requested_brands": ["리바로", "마운자로"],
        "resolved_brands": ["리바로"],
        "expected_cases": ["brands", "deep:리바로:general:C10A1:ubist"],
        "observations": [
            {"id": "brands", "candidate_status": 200, "reference_status": 200, "parity": True},
            {
                "id": "deep:리바로:general:C10A1:ubist",
                "candidate_status": 500,
                "reference_status": 200,
                "parity": False,
            },
        ],
    }

    result = check_latency_matrix_evidence(evidence, "failure-injection")

    assert result.exit_code == 1
    assert any("unresolved required brands" in detail for detail in result.details)
    assert any("candidate HTTP 500" in detail for detail in result.details)
    assert any("response mismatch" in detail for detail in result.details)
