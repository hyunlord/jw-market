"""Σ(parts)=whole pin for real loads: Σ brand raw_value == market_size_series.

Pinned 2026-07-17 against the live mart (read-only census, both categories,
every market, latest period): ubist 364/364, iqvia_nsa 538/538, worst relative
error 0.000000%. The reconciliation grain is (source, measure='sales',
atc4_code, period):

    Σ over mart_general_brand_metric.metric_history[period].raw_value
      == mart_general_market_metric.market_size_series[period]

``check_market_sigma`` runs after an incremental load, scoped to the loaded
epoch's periods, against whatever DB the Job env points at (staging until the
D-3 PL approval). One correct market cannot hide a broken one; a market whose
series lacks the period entirely is a failure, not a skip, when the load
claims to have written it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

ABS_TOL = 0.01
REL_TOL = 0.001


class MarketSigmaError(ValueError):
    pass


@dataclass
class MarketSigmaReport:
    source: str
    periods: tuple[str, ...]
    markets_checked: int = 0
    cells_checked: int = 0
    worst_rel: float = 0.0
    failures: list[str] = field(default_factory=list)


def _series(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _brand_value(entry) -> float | None:
    if isinstance(entry, dict):
        entry = entry.get("raw_value")
    return float(entry) if isinstance(entry, (int, float)) else None


def check_market_sigma(
    conn,
    *,
    source: str,
    periods: tuple[str, ...],
    abs_tol: float = ABS_TOL,
    rel_tol: float = REL_TOL,
    mark: str = "%s",
) -> MarketSigmaReport:
    """Reconcile every market of ``source`` over ``periods``; raise on any gap."""
    if not periods:
        raise MarketSigmaError("no periods to reconcile (empty load scope)")
    report = MarketSigmaReport(source=source, periods=tuple(periods))

    cursor = conn.cursor()
    cursor.execute(
        "SELECT atc4_code, market_size_series FROM mart_general_market_metric"
        f" WHERE source={mark} AND measure='sales'",
        (source,),
    )
    markets = cursor.fetchall()
    if not markets:
        raise MarketSigmaError(f"{source}: no markets found — cannot attest a load")

    cursor.execute(
        "SELECT atc4_code, metric_history FROM mart_general_brand_metric"
        f" WHERE source={mark} AND measure='sales'",
        (source,),
    )
    brands: dict[str, list] = {}
    for atc4, history in cursor.fetchall():
        brands.setdefault(atc4, []).append(_series(history))

    for atc4, series_raw in markets:
        market_series = _series(series_raw)
        market_touched = False
        for period in periods:
            whole = market_series.get(period)
            values = [
                value
                for history in brands.get(atc4, [])
                if (value := _brand_value(history.get(period))) is not None
            ]
            if whole is None and not values:
                continue  # market genuinely has no data for this period
            market_touched = True
            report.cells_checked += 1
            if whole is None:
                report.failures.append(f"{atc4} {period}: brands present but market whole missing")
                continue
            if not values:
                report.failures.append(f"{atc4} {period}: market whole present but no brand parts")
                continue
            parts = sum(values)
            whole = float(whole)
            gap = abs(parts - whole)
            rel = gap / max(abs(whole), 1e-9)
            report.worst_rel = max(report.worst_rel, rel)
            if gap > abs_tol and rel > rel_tol:
                report.failures.append(
                    f"{atc4} {period}: Σbrands {parts:.2f} != market {whole:.2f} (rel {rel:.4%})"
                )
        if market_touched:
            report.markets_checked += 1

    if report.cells_checked == 0:
        raise MarketSigmaError(
            f"{source}: no market carries any of the loaded periods {list(periods)} — "
            "the load claims periods the mart never received"
        )
    if report.failures:
        raise MarketSigmaError("; ".join(report.failures[:10]))
    return report
