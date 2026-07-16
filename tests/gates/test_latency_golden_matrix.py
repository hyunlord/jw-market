from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pipeline.scripts.gates.latency_matrix_cases import (
    LATENCY_MATRIX_PROVENANCE,
    build_latency_matrix_cases,
    resolved_brand_names,
)
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


def test_latency_matrix_cli_import_does_not_require_db_driver() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = """
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pymysql" or name.startswith("pymysql."):
        raise ModuleNotFoundError("blocked DB-only dependency")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
from pipeline.scripts.gates import release_acceptance

assert callable(release_acceptance.check_latency_matrix_evidence)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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


def _mock_filter_options_payload(
    case,
    overrides: dict[tuple[str, str], str] | None = None,
) -> dict[str, object]:
    query = parse_qs(urlsplit(case.path).query)
    brand = query["brand"][0]
    source = query["source"][0].lower()
    default_atc4 = {
        "리바로": "C10A1",
        "악템라": "L04A0",
        "가드렛": "A10N1",
        "라베칸": "A2B2",
    }
    atc4 = (overrides or {}).get((brand, source), default_atc4[brand])
    return {"market_id": atc4, "flagged_atc4": [atc4]}


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
    assert "filter_options:리바로:strategic:ubist:sales" in identifiers
    assert not any(identifier.startswith("filter_options:리바로:strategic_ml:") for identifier in identifiers)
    assert not any(identifier.startswith("filter_options:리바로:strategic_cd:") for identifier in identifiers)
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


def test_latency_matrix_cases_use_source_specific_filter_options_for_general_dynamic_requests() -> None:
    defaults = [
        {
            "brand": "라베칸",
            "atc_codes": ["A2B2"],
            "general_sources": ["UBIST", "IQVIA"],
        }
    ]
    payloads = {
        "라베칸": [
            {
                "brand": "라베칸",
                "general_sources": ["UBIST", "IQVIA"],
                "contexts": [
                    {"view_kind": "general", "market_id": "A02B2", "has_market_data": True},
                    {"view_kind": "general", "market_id": "A2B2", "has_market_data": True},
                ],
            }
        ]
    }
    filter_options = {
        "filter_options:라베칸:general:ubist:sales": {
            "market_id": "A2B2",
            "flagged_atc4": ["A2B2"],
        },
        "filter_options:라베칸:general:iqvia:sales": {
            "market_id": "A02B2",
            "flagged_atc4": ["A02B2"],
        },
    }

    cases = build_latency_matrix_cases(
        defaults,
        payloads,
        requested_brands=("라베칸",),
        filter_option_payloads=filter_options,
    )
    dynamic = {case.identifier: case for case in cases if case.identifier.startswith("dynamic:라베칸:")}

    assert dynamic["dynamic:라베칸:general:ubist:sales"].body["filters"]["atc4"] == ["A2B2"]
    assert dynamic["dynamic:라베칸:general:iqvia:sales"].body["filters"]["atc4"] == ["A02B2"]


def test_latency_matrix_brand_resolution_normalizes_whitespace_aliases() -> None:
    payloads = {
        "리바로브이": [
            {
                "brand": "리바로 브이",
                "general_sources": ["UBIST"],
                "contexts": [
                    {"view_kind": "general", "market_id": "C11A1", "has_market_data": True}
                ],
            }
        ],
        "위너프A+": [],
    }

    assert resolved_brand_names(payloads, ("리바로브이", "위너프A+")) == ("리바로브이",)


def test_latency_matrix_strategic_filter_options_use_public_strategic_contract_once_per_source() -> None:
    cases = build_latency_matrix_cases(
        DEFAULT_BRANDS,
        SEARCH_PAYLOADS,
        requested_brands=("리바로",),
    )
    strategic = [case for case in cases if case.identifier.startswith("filter_options:리바로:strategic:")]

    assert len(strategic) == 1
    assert "view=strategic" in strategic[0].path


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


def test_latency_matrix_cases_reject_group_scope_without_canonical_atc_membership() -> None:
    try:
        build_latency_matrix_cases(
            DEFAULT_BRANDS,
            SEARCH_PAYLOADS,
            requested_brands=("리바로",),
            group_scopes=(("group:unknown", "리바로"),),
        )
    except ValueError as exc:
        assert "group scope ATC membership unresolved: group:unknown" in str(exc)
    else:
        raise AssertionError("group scope without canonical ATC membership must fail closed")


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
        "provenance": LATENCY_MATRIX_PROVENANCE,
        "default_brands": ["리바로", "악템라", "가드렛"],
        "requested_brands": ["리바로", "악템라", "가드렛"],
        "resolved_brands": ["리바로", "악템라", "가드렛"],
        "context_resolved_brands": ["리바로", "악템라", "가드렛"],
        "default_only_brands": [],
        "excluded_reference_cases": [],
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


def test_latency_matrix_gate_rejects_overlapping_resolution_partitions() -> None:
    evidence = {
        "classification": "census",
        "provenance": LATENCY_MATRIX_PROVENANCE,
        "default_brands": ["리바로"],
        "requested_brands": ["리바로"],
        "resolved_brands": ["리바로"],
        "context_resolved_brands": ["리바로"],
        "default_only_brands": ["리바로"],
        "excluded_reference_cases": [],
        "required_cd_brands": ["악템라", "가드렛"],
        "required_group_scopes": [
            ["group:livalo_family", "리바로"],
            ["group:gardlet_family", "가드렛"],
        ],
        "expected_cases": ["brands"],
        "observations": [_passing_observation("brands")],
    }

    result = check_latency_matrix_evidence(evidence, "failure-injection")

    assert result.exit_code == 1
    assert any("resolution partitions overlap" in detail for detail in result.details)


def test_latency_matrix_runtime_uses_reference_population_and_masks_only_deep_timestamp() -> None:
    def requester(base_url, case, _timeout_seconds):
        if case.identifier == "brands":
            payload = DEFAULT_BRANDS
        elif case.identifier.startswith("brand_search:"):
            brand = parse_qs(urlsplit(case.path).query)["q"][0]
            payload = SEARCH_PAYLOADS[brand]
        elif case.identifier.startswith("filter_options:"):
            payload = _mock_filter_options_payload(case)
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
        max_workers=1,
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


def test_latency_matrix_runtime_builds_general_requests_from_reference_filter_options() -> None:
    default_brands = [
        *DEFAULT_BRANDS,
        {
            "brand": "라베칸",
            "atc_codes": ["A2B2"],
            "general_sources": ["UBIST", "IQVIA"],
        },
    ]
    payloads = {
        **SEARCH_PAYLOADS,
        "라베칸": [
            {
                "brand": "라베칸",
                "general_sources": ["UBIST", "IQVIA"],
                "contexts": [
                    {"view_kind": "general", "market_id": "A02B2", "has_market_data": True},
                    {"view_kind": "general", "market_id": "A2B2", "has_market_data": True},
                ],
            }
        ],
    }
    dynamic_atc4: dict[tuple[str, str], list[str]] = {}

    def requester(base_url, case, _timeout_seconds):
        if case.identifier == "brands":
            payload = default_brands
        elif case.identifier.startswith("brand_search:"):
            brand = parse_qs(urlsplit(case.path).query)["q"][0]
            payload = payloads[brand]
        elif case.identifier.startswith("filter_options:"):
            payload = _mock_filter_options_payload(case, {("라베칸", "iqvia"): "A02B2"})
        elif case.identifier.startswith("dynamic:라베칸:general:"):
            dynamic_atc4[(base_url, case.identifier)] = case.body["filters"]["atc4"]
            payload = {"value": case.identifier}
        elif case.identifier.startswith("deep:"):
            payload = {"value": case.identifier, "generated_at": base_url}
        elif case.identifier.startswith("brand_activity_group:topics:"):
            payload = {
                "data": {
                    "brands": [
                        {"brand_name": "리바로", "event_count": 1, "topic_shares": [{"topic_id": "T01"}]}
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
        max_workers=1,
        requester=requester,
        edge_brands=(),
    )
    result = check_latency_matrix_evidence(evidence, "unit")

    for base_url in ("http://candidate", "http://reference"):
        assert dynamic_atc4[(base_url, "dynamic:라베칸:general:ubist:sales")] == ["A2B2"]
        assert dynamic_atc4[(base_url, "dynamic:라베칸:general:iqvia:sales")] == ["A02B2"]
    assert result.exit_code == 0


def test_latency_matrix_runtime_excludes_reference_source_mismatch_from_serving_population() -> None:
    payloads = json.loads(json.dumps(SEARCH_PAYLOADS, ensure_ascii=False))
    payloads["리바로"][0]["general_sources"] = ["UBIST", "IQVIA"]
    candidate_invalid_called = False

    def requester(base_url, case, _timeout_seconds):
        nonlocal candidate_invalid_called
        if case.identifier == "brands":
            payload = DEFAULT_BRANDS
            status = 200
        elif case.identifier.startswith("brand_search:"):
            brand = parse_qs(urlsplit(case.path).query)["q"][0]
            payload = payloads[brand]
            status = 200
        elif case.identifier.startswith("filter_options:"):
            payload = _mock_filter_options_payload(case)
            status = 200
        elif case.identifier == "deep:리바로:general:C10A1:iqvia":
            if base_url == "http://candidate":
                candidate_invalid_called = True
                raise AssertionError("candidate must not be called for a reference-invalid deep context")
            payload = {"detail": {"error": "source_not_available"}}
            status = 422
        elif case.identifier.startswith("deep:"):
            payload = {"value": case.identifier, "generated_at": base_url}
            status = 200
        elif case.identifier.startswith("brand_activity_group:topics:"):
            payload = {
                "data": {
                    "brands": [
                        {"brand_name": "리바로", "event_count": 1, "topic_shares": [{"topic_id": "T01"}]}
                    ]
                }
            }
            status = 200
        else:
            payload = {"value": case.identifier}
            status = 200
        return RawResponse(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        )

    evidence = collect_latency_matrix_evidence(
        "http://candidate",
        "http://reference",
        timeout_seconds=1.0,
        max_workers=1,
        requester=requester,
        edge_brands=(),
    )

    assert candidate_invalid_called is False
    assert "deep:리바로:general:C10A1:iqvia" not in evidence["expected_cases"]
    assert evidence["excluded_reference_cases"] == [
        {
            "id": "deep:리바로:general:C10A1:iqvia",
            "reason": "source_not_available",
            "reference_status": 422,
        }
    ]


def test_latency_matrix_runtime_keeps_default_membership_when_search_is_empty() -> None:
    default_brands = [*DEFAULT_BRANDS, {"brand": "위너프A+", "general_sources": ["UBIST"]}]

    def requester(_base_url, case, _timeout_seconds):
        if case.identifier == "brands":
            payload = default_brands
        elif case.identifier == "brand_search:위너프A+":
            payload = []
        elif case.identifier.startswith("brand_search:"):
            brand = parse_qs(urlsplit(case.path).query)["q"][0]
            payload = SEARCH_PAYLOADS[brand]
        elif case.identifier.startswith("filter_options:"):
            payload = _mock_filter_options_payload(case)
        elif case.identifier.startswith("deep:"):
            payload = {"value": case.identifier, "generated_at": "masked"}
        elif case.identifier.startswith("brand_activity_group:topics:"):
            payload = {
                "data": {
                    "brands": [
                        {"brand_name": "리바로", "event_count": 1, "topic_shares": [{"topic_id": "T01"}]}
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
        max_workers=1,
        requester=requester,
        edge_brands=(),
    )

    assert "위너프A+" in evidence["resolved_brands"]
    assert "위너프A+" in evidence["default_brands"]
    assert "위너프A+" not in evidence["context_resolved_brands"]
    assert evidence["default_only_brands"] == ["위너프A+"]
    assert not any(":위너프A+:" in identifier for identifier in evidence["expected_cases"])


def test_latency_matrix_runtime_rejects_parallel_census() -> None:
    try:
        collect_latency_matrix_evidence(
            "http://candidate",
            "http://reference",
            timeout_seconds=1.0,
            max_workers=2,
            requester=lambda *_args: RawResponse(status=500, body=b""),
            edge_brands=(),
        )
    except ValueError as exc:
        assert "max_workers=1" in str(exc)
    else:
        raise AssertionError("parallel latency census must fail closed")


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
        "provenance": LATENCY_MATRIX_PROVENANCE,
        "default_brands": ["리바로", "악템라", "가드렛"],
        "requested_brands": ["리바로", "악템라", "가드렛"],
        "resolved_brands": ["리바로", "악템라", "가드렛"],
        "context_resolved_brands": ["리바로", "악템라", "가드렛"],
        "default_only_brands": [],
        "excluded_reference_cases": [],
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
        "provenance": LATENCY_MATRIX_PROVENANCE,
        "default_brands": ["리바로"],
        "requested_brands": ["리바로", "마운자로"],
        "resolved_brands": ["리바로"],
        "context_resolved_brands": ["리바로"],
        "default_only_brands": [],
        "excluded_reference_cases": [],
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
