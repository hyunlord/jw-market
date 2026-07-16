from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from jw_chat_agent_poc.ops.deep_rollout_gate import (
    ExactAmountContract,
    exact_amount_gate,
    concentration_gate,
    derived_parity_gate,
    provenance_gate,
    strict_log_gate,
)


@dataclass(frozen=True)
class ParityReport:
    checked: int
    population: int
    failures: tuple[str, ...]
    exit_code: int
    classification: str = "census"


LIVARO_APRIL = ExactAmountContract(
    expected_amount=Decimal("83.184115"),
    period_terms=("2025-04", "2025년 4월"),
    subject_terms=("리바로",),
    metric_terms=("매출",),
)


def test_exact_amount_gate_accepts_only_the_scoped_approved_value() -> None:
    result = exact_amount_gate(
        "market_month_response",
        "리바로 2025-04 매출은 83.184115억원입니다.",
        LIVARO_APRIL,
    )

    assert result.exit_code == 0
    assert result.checked == result.population == 1
    assert result.details["scoped_amounts_억원"] == ["83.184115"]


@pytest.mark.parametrize(
    "answer",
    (
        (
            "리바로 2025-04 매출은 83.184115억원이지만 "
            "실제 리바로 2025-04 매출은 84억원입니다."
        ),
        "리바로 2025-04 매출은 83.184115억원입니다.\n하지만 실제 값은 84억원입니다.",
        "경쟁사 2025-04 매출은 83.184115억원입니다. 리바로 2025-04 매출은 84억원입니다.",
    ),
)
def test_exact_amount_gate_rejects_contradictory_or_misattributed_values(answer: str) -> None:
    result = exact_amount_gate("market_month_response", answer, LIVARO_APRIL)

    assert result.exit_code == 1
    assert result.failures


def test_concentration_gate_rejects_any_contradictory_metric_value() -> None:
    accepted = concentration_gate(
        "market_concentration_response",
        "HHI는 253.62이고 CR5는 29.52%입니다.",
        expected={"HHI": Decimal("253.62"), "CR5": Decimal("29.52")},
    )
    rejected = concentration_gate(
        "market_concentration_response",
        "HHI 253.62는 이전값이고 실제 HHI는 999입니다. "
        "CR5 29.52%는 이전값이고 실제 CR5는 99%입니다.",
        expected={"HHI": Decimal("253.62"), "CR5": Decimal("29.52")},
    )

    assert accepted.exit_code == 0
    assert rejected.exit_code == 1
    assert rejected.details["observed"]["HHI"] == ["253.62", "999"]
    assert rejected.details["observed"]["CR5"] == ["29.52", "99"]


def test_derived_parity_gate_accepts_a_dynamic_nonempty_census() -> None:
    result = derived_parity_gate(
        ParityReport(checked=123, population=123, failures=(), exit_code=0)
    )

    assert result.exit_code == 0
    assert result.checked == result.population == 123


@pytest.mark.parametrize(
    "report",
    (
        ParityReport(checked=0, population=0, failures=(), exit_code=0),
        ParityReport(checked=122, population=123, failures=(), exit_code=0),
        ParityReport(checked=123, population=123, failures=("drift",), exit_code=1),
    ),
)
def test_derived_parity_gate_rejects_empty_incomplete_or_failed_census(
    report: ParityReport,
) -> None:
    assert derived_parity_gate(report).exit_code == 1


def test_strict_log_gate_accepts_successful_silent_pods() -> None:
    result = strict_log_gate(
        "startup complete\n",
        (
            {"pod": "chat-a", "exit_code": 0, "log_bytes": 17, "stderr_bytes": 0},
            {"pod": "chat-b", "exit_code": 0, "log_bytes": 0, "stderr_bytes": 0},
        ),
        desired_replicas=2,
    )

    assert result.exit_code == 0
    assert result.checked == result.population == 2


@pytest.mark.parametrize(
    ("text", "statuses", "desired"),
    (
        ("", (), 0),
        (
            "startup complete\n",
            ({"pod": "chat-a", "exit_code": 0, "log_bytes": 17, "stderr_bytes": 0},),
            2,
        ),
        (
            "startup complete\nERROR injected\n",
            (
                {"pod": "chat-a", "exit_code": 0, "log_bytes": 17, "stderr_bytes": 0},
                {"pod": "chat-b", "exit_code": 0, "log_bytes": 17, "stderr_bytes": 0},
            ),
            2,
        ),
        (
            '10.0.0.1 - "GET /health HTTP/1.1" 500 Internal Server Error\n',
            (
                {"pod": "chat-a", "exit_code": 0, "log_bytes": 65, "stderr_bytes": 0},
                {"pod": "chat-b", "exit_code": 0, "log_bytes": 0, "stderr_bytes": 0},
            ),
            2,
        ),
        (
            "startup complete\n",
            (
                {"pod": "chat-a", "exit_code": 0, "log_bytes": 17, "stderr_bytes": 0},
                {"pod": "chat-b", "exit_code": 1, "log_bytes": 0, "stderr_bytes": 29},
            ),
            2,
        ),
    ),
)
def test_strict_log_gate_rejects_missing_error_or_retrieval_failure(
    text: str,
    statuses: tuple[dict[str, object], ...],
    desired: int,
) -> None:
    assert strict_log_gate(text, statuses, desired_replicas=desired).exit_code == 1


def test_provenance_gate_requires_every_declared_field() -> None:
    expected = {
        "classification": "tracked_golden",
        "truth_basis_status": "confirmed",
        "commit": "abc123",
        "path": "tests/goldens/market_goldens.json",
    }

    assert provenance_gate("market_provenance", expected, expected).exit_code == 0
    for key in expected:
        mutated = dict(expected)
        mutated[key] = "wrong"
        result = provenance_gate("market_provenance", mutated, expected)
        assert result.exit_code == 1, key


def test_acceptance_payload_keeps_checked_and_population_separate() -> None:
    result = derived_parity_gate(
        ParityReport(checked=7, population=7, failures=(), exit_code=0)
    )

    assert result.to_dict() == {
        "gate": "derived_parity",
        "classification": "census",
        "checked": 7,
        "population": 7,
        "missing": "fail",
        "tolerance": "exact",
        "failures": 0,
        "failure_reasons": [],
        "exit_code": 0,
        "environment": "live",
        "details": {},
    }
