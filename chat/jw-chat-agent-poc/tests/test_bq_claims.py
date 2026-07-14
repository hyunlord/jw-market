from __future__ import annotations

from collections.abc import Callable

import pytest

from jw_chat_agent_poc.orchestrator.bq_claims import (
    BQClaimError,
    EvidenceRef,
    GroundedClaim,
    conditional_forecast_claim,
    event_identity_claim,
    news_identity_claim,
    numeric_so_what_claim,
    number_claim,
    temporal_overlap_claim,
    verify_claim_surface,
)


def test_number_claim_keeps_exact_reference_and_number() -> None:
    claim = number_claim(
        value=29.52,
        refs=(EvidenceRef(source="UBIST", kind="number", identity="CR5", period="2026-05"),),
    )

    assert claim.kind == "number"
    assert claim.text == "29.52%"
    assert claim.evidence_refs[0].identity == "CR5"
    verify_claim_surface(claim, "CR5 29.52%")


def test_temporal_overlap_claim_accepts_overlapping_periods() -> None:
    claim = temporal_overlap_claim(
        claim_period=("2026-01", "2026-03"),
        refs=(
            EvidenceRef(source="UBIST", kind="time_window", identity="2026-01", period="2026-01"),
            EvidenceRef(source="UBIST", kind="time_window", identity="2026-03", period="2026-03"),
        ),
    )

    assert claim.text == "2026-01~2026-03"
    verify_claim_surface(claim, "2026-01~2026-03 overlap")


def test_conditional_forecast_claim_requires_uncertainty() -> None:
    claim = conditional_forecast_claim(
        value=8.4,
        condition="if launch holds",
        uncertainty="±1.2%",
        refs=(EvidenceRef(source="IQVIA", kind="forecast", identity="launch", period="2026-06"),),
    )

    assert claim.condition == "if launch holds"
    assert claim.uncertainty == "±1.2%"
    verify_claim_surface(claim, "8.4 if launch holds with ±1.2%")


@pytest.mark.parametrize(
    ("current", "baseline", "expected"),
    (
        (84.93, 79.12, "+5.81"),
        (79.12, 84.93, "-5.81"),
    ),
)
def test_numeric_so_what_claim_emits_grounded_delta(current: float, baseline: float, expected: str) -> None:
    claim = numeric_so_what_claim(
        current=current,
        baseline=baseline,
        refs=(EvidenceRef(source="UBIST", kind="number", identity="sales", period="2026-05"),),
    )

    assert expected in claim.text
    verify_claim_surface(claim, claim.text)


@pytest.mark.parametrize(
    ("factory", "surface"),
    (
        (
            lambda: number_claim(
                value=0.53,
                refs=(EvidenceRef(source="UBIST", kind="number", identity="MS", period="2026-05"),),
            ),
            "MS 0.53%",
        ),
        (
            lambda: number_claim(
                value=3.76,
                refs=(EvidenceRef(source="UBIST", kind="number", identity="share", period="2026-05"),),
            ),
            "share 3.76%",
        ),
        (
            lambda: temporal_overlap_claim(
                claim_period=("2026-04", "2026-06"),
                refs=(
                    EvidenceRef(source="UBIST", kind="time_window", identity="2026-04", period="2026-04"),
                    EvidenceRef(source="UBIST", kind="time_window", identity="2026-06", period="2026-06"),
                ),
            ),
            "2026-04~2026-06 overlap",
        ),
        (
            lambda: conditional_forecast_claim(
                value=7.2,
                condition="if reimbursement holds",
                uncertainty="±0.8%",
                refs=(EvidenceRef(source="IQVIA", kind="forecast", identity="reimbursement", period="2026-07"),),
            ),
            "7.2 if reimbursement holds with ±0.8%",
        ),
        (
            lambda: news_identity_claim(
                identity="2026-05 입찰 기사",
                refs=(EvidenceRef(source="NEWS", kind="news", identity="2026-05 입찰 기사", period="2026-05"),),
            ),
            "2026-05 입찰 기사",
        ),
        (
            lambda: event_identity_claim(
                identity="IR meeting (Q2)",
                refs=(EvidenceRef(source="EVENTS", kind="event", identity="IR meeting (Q2)", period="2026-05"),),
            ),
            "IR meeting (Q2)",
        ),
        (
            lambda: numeric_so_what_claim(
                current=84.93,
                baseline=80.00,
                refs=(EvidenceRef(source="UBIST", kind="number", identity="sales", period="2026-05"),),
            ),
            "+4.93",
        ),
        (
            lambda: numeric_so_what_claim(
                current=80.00,
                baseline=84.93,
                refs=(EvidenceRef(source="UBIST", kind="number", identity="sales", period="2026-05"),),
            ),
            "-4.93",
        ),
        (
            lambda: number_claim(
                value=29.52,
                refs=(EvidenceRef(source="UBIST", kind="number", identity="CR5", period="2026-05"),),
            ),
            "CR5 29.52%",
        ),
        (
            lambda: conditional_forecast_claim(
                value=6.0,
                condition="if scenario B persists",
                uncertainty="±2.0%",
                refs=(EvidenceRef(source="MODEL", kind="forecast", identity="scenario B", period="2026-08"),),
            ),
            "6 if scenario B persists ±2.0%",
        ),
    ),
)
def test_grounded_claim_accepts_normal_false_positive_cases(factory: Callable[[], GroundedClaim], surface: str) -> None:
    claim = factory()

    verify_claim_surface(claim, surface)


@pytest.mark.parametrize("case", range(30))
def test_grounded_number_claim_accepts_thirty_normal_surfaces(case: int) -> None:
    value = case + 0.25
    suffix = chr(ord("a") + case % 26) * (case // 26 + 1)
    claim = number_claim(
        value=value,
        refs=(
            EvidenceRef(
                source="UBIST",
                kind="number",
                identity=f"normal-case-{suffix}",
                period=f"2026-{case % 12 + 1:02d}",
            ),
        ),
    )

    verify_claim_surface(claim, f"{claim.identity} 정상 수치 {claim.text}")


def test_numeric_so_what_claim_rejects_missing_values_instead_of_zero_or_minus_hundred() -> None:
    refs = (EvidenceRef(source="UBIST", kind="number", identity="sales", period="2026-05"),)

    with pytest.raises(BQClaimError, match="missing"):
        numeric_so_what_claim(current=None, baseline=84.93, refs=refs)

    with pytest.raises(BQClaimError, match="missing"):
        numeric_so_what_claim(current=84.93, baseline=None, refs=refs)


@pytest.mark.parametrize(
    "claim_factory",
    (
        lambda: news_identity_claim(
            identity="known article",
            refs=(
                EvidenceRef(source="NEWS", kind="news", identity="known article", period="2026-05"),
                EvidenceRef(source="IQVIA", kind="news", identity="known article", period="2026-05"),
            ),
        ),
        lambda: event_identity_claim(
            identity="known event",
            refs=(
                EvidenceRef(source="EVENTS", kind="event", identity="known event", period="2026-05"),
                EvidenceRef(source="IQVIA", kind="event", identity="known event", period="2026-05"),
            ),
        ),
    ),
)
def test_identity_claim_rejects_source_aggregation(claim_factory: Callable[[], GroundedClaim]) -> None:
    with pytest.raises(BQClaimError, match="source aggregation"):
        claim_factory()


def test_verify_claim_surface_rejects_unknown_number() -> None:
    claim = number_claim(
        value=29.52,
        refs=(EvidenceRef(source="UBIST", kind="number", identity="CR5", period="2026-05"),),
    )

    with pytest.raises(BQClaimError, match="unknown number"):
        verify_claim_surface(claim, "CR5 29.53%")


def test_verify_claim_surface_rejects_condition_dropping() -> None:
    claim = conditional_forecast_claim(
        value=8.4,
        condition="if launch holds",
        uncertainty="±1.2%",
        refs=(EvidenceRef(source="IQVIA", kind="forecast", identity="launch", period="2026-06"),),
    )

    with pytest.raises(BQClaimError, match="condition"):
        verify_claim_surface(claim, "8.4 with ±1.2%")


def test_verify_claim_surface_rejects_unsupported_substitution() -> None:
    claim = news_identity_claim(
        identity="2026-05 입찰 기사",
        refs=(EvidenceRef(source="NEWS", kind="news", identity="2026-05 입찰 기사", period="2026-05"),),
    )

    with pytest.raises(BQClaimError, match="substitution"):
        verify_claim_surface(claim, "2026-05 입찰 event")
