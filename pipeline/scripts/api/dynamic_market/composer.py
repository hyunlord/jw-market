"""Thin response composer for the dynamic market route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.scripts.api.composers.number_format import deep_format_numbers
from pipeline.scripts.api.dynamic_market.cause_payload import build_cause_payload
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, MarketDefinition, PeriodRange


@dataclass(frozen=True, slots=True)
class ResponseComposer:
    """Serialize runtime metrics with the same field tree as ``/api/cause``."""

    def compose(
        self,
        *,
        definition: MarketDefinition,
        metrics: AggregatedMetrics,
        period_range: PeriodRange = PeriodRange(),
    ) -> dict[str, Any]:
        """Return a JSON-ready cause-compatible dynamic-market response."""

        return deep_format_numbers(
            build_cause_payload(definition=definition, metrics=metrics, period_range=period_range)
        )
