"""K3 — market-scope axis exemption driven by fact origin, not fact content.

Three of the four binding axes already exempt facts that cannot answer them.
``scope_matches`` did not, so a fact from a source whose schema has no
``market_id`` field at all was treated the same as a market fact that simply
lost its market identity. This suite pins both halves of that distinction.

Stage 0 of the round required a test that actually walks the HIRA
reimbursement path into ``scope_matches`` -- the earlier measurement round
never observed that population, so the exemption target was known only by
reading code.
"""
from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.provenance import (
    EvidenceFact,
    evidence_from_calls,
)
from jw_chat_agent_poc.service.evidence_binding_rules import scope_matches
from jw_chat_agent_poc.tool_use.reimbursement_evidence import (
    project_reimbursement_evidence,
)

EXPECTED_MKT = frozenset({"ml_006"})

_REIMB_SUBJECT = "리바로"
_REIMB_METRIC = "HIRA 보험인정기준 원문 (AI 요약·해석·재구성 없음)"
_REIMB_SOURCE = "심사평가원(HIRA) 보험인정기준"
_REIMB_LOCATOR = "고시 제2024-1호 · 이상지질혈증 투여 기준"
_REIMB_RENDERED = (
    f"- {_REIMB_SUBJECT} (2024-01-01): {_REIMB_METRIC} = {_REIMB_LOCATOR} "
    f"[{_REIMB_SOURCE}]"
)


def _reimbursement_call() -> dict:
    """A hira_reimbursement_criteria call shaped like the real envelope."""
    return {
        "tool": "hira_reimbursement_criteria",
        "status": "ok",
        "render_data": {
            "ok": True,
            "evidence": [
                {
                    "fact_id": "hira_reimbursement:리바로:2024-01-01",
                    "subject": _REIMB_SUBJECT,
                    "metric": _REIMB_METRIC,
                    "value": None,
                    "unit": None,
                    "period": "2024-01-01",
                    "source_name": _REIMB_SOURCE,
                    "source_locator": _REIMB_LOCATOR,
                    "raw_ref": None,
                }
            ],
        },
    }


def _projected_reimbursement_fact() -> EvidenceFact:
    projected = project_reimbursement_evidence(
        [_reimbursement_call()], _REIMB_RENDERED
    )
    assert projected, "stage 0 fixture did not reach the reimbursement projection"
    return EvidenceFact(**projected[0])


# --------------------------------------------------------------------------
# Stage 0 -- the exemption target is reachable, and it is the failing one
# --------------------------------------------------------------------------
def test_stage0_reimbursement_path_reaches_the_binder() -> None:
    """The projection produces a binder fact with no market coordinates."""
    fact = _projected_reimbursement_fact()

    assert fact.tool == "hira_reimbursement_criteria"
    assert fact.view == ""
    assert fact.market_id == ""
    # the envelope schema has no market_id field, so the projection could not
    # have supplied one even in principle
    assert fact.market_scope_capable is False


def test_stage0_reimbursement_fact_is_exempt_from_the_market_axis() -> None:
    fact = _projected_reimbursement_fact()
    assert scope_matches(fact, frozenset(), EXPECTED_MKT) is True


# --------------------------------------------------------------------------
# Stage 2 -- market builders keep their market identity requirement
# --------------------------------------------------------------------------
def _market_call(*, market_id: str) -> dict:
    render: dict = {
        "brand": "리바로",
        "period": "2024-01",
        "market_size_recent_krw": 213925000000,
    }
    if market_id:
        render["market_id"] = market_id
    return {"tool": "get_brand_metric", "status": "ok", "render_data": render}


def _market_facts(*, market_id: str) -> tuple[EvidenceFact, ...]:
    return evidence_from_calls([_market_call(market_id=market_id)], "")


def _market_size_fact(*, market_id: str) -> EvidenceFact:
    for fact in _market_facts(market_id=market_id):
        if fact.metric == "시장규모":
            return fact
    raise AssertionError("market-size fact was not built")


def test_market_builder_facts_are_scope_capable() -> None:
    fact = _market_size_fact(market_id="ml_006")
    assert fact.market_scope_capable is True


def test_true_market_gap_stays_blocked() -> None:
    """The load-bearing case.

    A market builder that can express market_id but did not is a genuine gap.
    Measurement found 60 such facts; exempting them was the failure mode the
    previous round stopped on.
    """
    fact = _market_size_fact(market_id="")

    assert fact.market_scope_capable is True
    assert fact.market_id == ""
    assert scope_matches(fact, frozenset(), EXPECTED_MKT) is False


def test_foreign_market_id_stays_blocked() -> None:
    fact = _market_size_fact(market_id="ml_999")
    assert scope_matches(fact, frozenset(), EXPECTED_MKT) is False


def test_matching_market_id_still_passes() -> None:
    fact = _market_size_fact(market_id="ml_006")
    assert scope_matches(fact, frozenset(), EXPECTED_MKT) is True


def test_no_expected_market_ids_keeps_legacy_behavior() -> None:
    for market_id in ("", "ml_006", "ml_999"):
        fact = _market_size_fact(market_id=market_id)
        assert scope_matches(fact, frozenset(), frozenset()) is True


# --------------------------------------------------------------------------
# Boundary matrix (계열 6)
# --------------------------------------------------------------------------
def _fact(*, capable: bool, market_id: str) -> EvidenceFact:
    return EvidenceFact(
        fact_id="x", label="l", value="1", source="s", tool="t", path="p",
        period="2024-01", allowed_numbers=("1",), entity="리바로",
        metric="매출", unit="억원", source_grade="AUTHORITATIVE",
        view="", market_id=market_id, market_scope_capable=capable,
    )


@pytest.mark.parametrize(
    "capable,market_id,expected",
    [
        (True, "", False),          # genuine gap -> blocked
        (False, "", True),          # cannot express -> exempt
        (True, "ml_006", True),     # matches
        (True, "ml_999", False),    # foreign
        (False, "ml_006", True),    # not capable but present -> normal compare
        (False, "ml_999", False),   # not capable but present and foreign
    ],
)
def test_boundary_matrix(capable: bool, market_id: str, expected: bool) -> None:
    assert scope_matches(_fact(capable=capable, market_id=market_id),
                         frozenset(), EXPECTED_MKT) is expected


# --------------------------------------------------------------------------
# Structural guard -- the forgotten-builder risk
# --------------------------------------------------------------------------
def test_every_market_id_call_site_declares_scope_capability() -> None:
    """default=False means a new market builder that forgets the flag would be
    silently exempted. Pin the two sets together so that cannot happen quietly.
    """
    import re
    from pathlib import Path

    import jw_chat_agent_poc.orchestrator.provenance as provenance

    source = Path(provenance.__file__).read_text(encoding="utf-8")

    def call_bodies(text: str, needle: str) -> list[str]:
        """Argument text of each call, delimited by balanced parentheses."""
        bodies = []
        for match in re.finditer(re.escape(needle), text):
            start = match.end()
            depth = 1
            for index in range(start, len(text)):
                char = text[index]
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        bodies.append(text[start:index])
                        break
        return bodies

    bodies = call_bodies(source, "_fact(")
    # sanity: the parser must actually find the known call sites
    assert len(bodies) >= 7, f"call-site parser found only {len(bodies)} _fact() calls"

    offenders = [
        body.strip().splitlines()[:2]
        for body in bodies
        if re.search(r"\bmarket_id\s*=", body)
        and "market_scope_capable=True" not in body
    ]
    assert not offenders, (
        "a _fact() call site passes market_id= without declaring "
        f"market_scope_capable=True: {offenders}"
    )
