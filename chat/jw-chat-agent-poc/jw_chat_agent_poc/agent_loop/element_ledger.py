"""Per-element accounting for a request.

A question can ask for several things at once. The BQ slot extractor already
splits a question into metrics and modifiers and already picks the matching
contract, so this module reuses that decomposition rather than inventing a
second one. It exists so that two decisions stop being taken on the whole
request at once:

* a typed stop belongs to the element that triggered it, not to the request;
* a disposition should describe what was delivered, not whether the answer
  body happened to be non-empty.
"""

from __future__ import annotations

from typing import Any, Final

from jw_chat_agent_poc.agent_loop.bq_slots import (
    contract_id_for_slots,
    extract_bq_slots,
    requested_prescription_metric,
)


SATISFIED: Final = "satisfied"
UNSUPPORTED: Final = "unsupported"
FAILED: Final = "failed"

# Contracts whose defining shape is multi-period. The market-scope shortcut
# answers a single point in time, so it cannot serve these.
_MULTI_PERIOD_CONTRACTS: Final = frozenset({"A1"})


def _slots(question: str):
    return extract_bq_slots(question, brand="", period="")


def request_metrics(question: str) -> tuple[str, ...]:
    """The metric elements the slot extractor found in the question."""
    return _slots(question).metrics


def supported_metrics_beside_prescription(question: str) -> tuple[str, ...]:
    """Metric elements that survive when the prescription element is stopped.

    'market' is dropped because it is the broad marker that also fires for
    'market_size'; keeping both would report one ask as two elements.
    """
    metrics = request_metrics(question)
    return tuple(
        metric
        for metric in metrics
        if metric != "prescription"
        and not (metric == "market" and "market_size" in metrics)
    )


def market_scope_defers_to_contract(question: str) -> bool:
    """True when the slots picked a contract the single-period shortcut cannot serve.

    This reads the contract the extractor already chose instead of adding
    another keyword list to the routing regexes; the extractor's modifier
    vocabulary is the single source of truth for whether a trend was asked for.
    """
    return contract_id_for_slots(_slots(question)) in _MULTI_PERIOD_CONTRACTS


def build_element_ledger(
    question: str,
    *,
    satisfied: tuple[str, ...] = (),
    unsupported: tuple[str, ...] = (),
    failed: tuple[str, ...] = (),
) -> tuple[dict[str, Any], ...]:
    """One entry per requested element, in the extractor's own order."""
    status_by_metric: dict[str, str] = {}
    for metric in satisfied:
        status_by_metric[metric] = SATISFIED
    for metric in unsupported:
        status_by_metric[metric] = UNSUPPORTED
    for metric in failed:
        status_by_metric[metric] = FAILED
    ordered = [metric for metric in request_metrics(question) if metric in status_by_metric]
    ordered.extend(metric for metric in status_by_metric if metric not in ordered)
    return tuple(
        {"element": metric, "status": status_by_metric[metric]} for metric in ordered
    )


def disposition_from_ledger(ledger: tuple[dict[str, Any], ...]) -> str | None:
    """Aggregate element outcomes into a disposition.

    Returns None when there is nothing to aggregate, so that a caller without
    a ledger keeps whatever it would otherwise have decided.
    """
    statuses = [str(entry.get("status") or "") for entry in ledger]
    if not statuses:
        return None
    if all(status == SATISFIED for status in statuses):
        return "answered"
    if any(status == SATISFIED for status in statuses):
        return "partial"
    return "unavailable"


def prescription_element_is_deferrable(question: str) -> bool:
    """True when a prescription stop would silence other, answerable elements."""
    return (
        requested_prescription_metric(question) is not None
        and bool(supported_metrics_beside_prescription(question))
    )
