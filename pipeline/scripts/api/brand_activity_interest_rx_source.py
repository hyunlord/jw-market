from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_csd_shared import JsonMap, ViewConfig, text
from pipeline.scripts.api.brand_activity_interest_rx_config import CSD_TOTAL_CHANNEL
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


class InterestRxSourceError(RuntimeError):
    """Raised when source table windows are unavailable."""


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    """Applied month window and its dynamic default evidence."""

    start: str
    end: str
    default_start: str
    default_end: str
    source: str


@dataclass(frozen=True, slots=True)
class KeywordQuery:
    """Keyword-stage aggregation query parameters."""

    period: PeriodWindow
    view: ViewConfig
    market_id: str
    product_codes: tuple[str, ...]
    visit_location: str
    specialty: str


@dataclass(frozen=True, slots=True)
class DetailingQuery:
    """CSD product-detail aggregation query parameters."""

    csd_market: str
    period: PeriodWindow


def dynamic_period_window() -> PeriodWindow:
    """Return the keyword/CSD overlapping month window."""

    rows = db.fetch_all(
        f"""
        SELECT 'keyword' AS source, MIN(period_ym) AS min_period, MAX(period_ym) AS max_period
        FROM {quote_identifier(config.brand_activity_db_name)}.`km_keyword_event_stage`
        UNION ALL
        SELECT 'csd' AS source, MIN(period_ym) AS min_period, MAX(period_ym) AS max_period
        FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage`
        WHERE jw_channel = %s
        """,
        (CSD_TOTAL_CHANNEL,),
    )
    bounds = {text(row.get("source")): (text(row.get("min_period")), text(row.get("max_period"))) for row in rows}
    keyword = bounds.get("keyword", ("", ""))
    csd = bounds.get("csd", ("", ""))
    if not all((*keyword, *csd)):
        raise InterestRxSourceError("keyword and CSD period bounds are required")
    start = max(keyword[0], csd[0])
    end = min(keyword[1], csd[1])
    if start > end:
        raise InterestRxSourceError("keyword and CSD periods do not overlap")
    return PeriodWindow(start, end, start, end, "dynamic_overlap")


def period_for_request(period_start: str, period_end: str, default: PeriodWindow) -> PeriodWindow:
    """Apply request period overrides to the dynamic default window."""

    start = period_start or default.start
    end = period_end or default.end
    if start > end:
        raise InterestRxSourceError("period_start must be earlier than or equal to period_end")
    source = "override" if period_start or period_end else default.source
    return PeriodWindow(start, end, default.default_start, default.default_end, source)


def fetch_keyword_rows(query: KeywordQuery) -> list[JsonMap]:
    """Fetch keyword-stage distributions for a matrix query."""

    market_clause, market_params = _market_clause(query.view, query.market_id, query.product_codes)
    clauses = ["period_ym BETWEEN %s AND %s", market_clause]
    params: list[Any] = [query.period.start, query.period.end, *market_params]
    if query.visit_location != "전체":
        clauses.append("visit_location = %s")
        params.append(query.visit_location)
    if query.specialty != "전체":
        clauses.append("specialty = %s")
        params.append(query.specialty)
    return db.fetch_all(
        f"""
        SELECT product_name, interest, prescription_frequency, prescription_evolution, COUNT(*) AS event_count
        FROM {quote_identifier(config.brand_activity_db_name)}.`km_keyword_event_stage`
        WHERE {" AND ".join(clauses)}
        GROUP BY product_name, interest, prescription_frequency, prescription_evolution
        """,
        tuple(params),
    )


def fetch_detailing_rows(query: DetailingQuery) -> list[JsonMap]:
    """Fetch CSD TOTAL product-details summed over the matrix period."""

    return db.fetch_all(
        f"""
        SELECT master_product, SUM(product_details) AS detailing
        FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage`
        WHERE market = %s AND jw_channel = %s AND period_ym BETWEEN %s AND %s
        GROUP BY master_product
        """,
        (query.csd_market, CSD_TOTAL_CHANNEL, query.period.start, query.period.end),
    )


def _market_clause(view: ViewConfig, market_id: str, product_codes: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    if product_codes:
        placeholders = ", ".join(["%s"] * len(product_codes))
        market_key = (
            view.market_key.replace("atc4_code", "therapeutic_class")
            .replace("ml_id", "therapeutic_class")
            .replace("cd_market_id", "therapeutic_class")
        )
        return f"({market_key} = %s OR product_name IN ({placeholders}))", (market_id, *product_codes)
    return "therapeutic_class = %s", (market_id,)
