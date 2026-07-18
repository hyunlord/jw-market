from __future__ import annotations

from decimal import Decimal
from typing import Any

Call = dict[str, Any]


def build_causal_analysis_call(calls: list[Call]) -> Call | None:
    market = _market_evidence(calls)
    activity = _activity_evidence(calls, market.get("UBIST"))
    patients = _patient_evidence(calls)
    news = _news_evidence(calls)
    ledger = [*market.values(), *activity["ledger"], *patients["ledger"], *news["ledger"]]
    if not ledger:
        return None

    insights: list[str] = []
    ubist = market.get("UBIST")
    iqvia = market.get("IQVIA NSA")
    if ubist and iqvia:
        if ubist["period"] == iqvia["period"]:
            delta = _decimal(iqvia["value_krw"]) - _decimal(ubist["value_krw"])
            insights.append(
                f"{ubist['period']} IQVIA NSA와 UBIST 매출 차이는 {delta / Decimal('100000000'):,.2f}억원이며 "
                "두 출처는 합산하지 않습니다."
            )
        else:
            insights.append(
                f"UBIST({ubist['period']})와 IQVIA NSA({iqvia['period']})의 기준기간이 달라 "
                "값을 나란히 제시하고 차이는 계산하지 않습니다."
            )
    if activity["insight"]:
        insights.append(activity["insight"])
    if patients["insight"]:
        insights.append(patients["insight"])
    if news["refs"]:
        item = news["refs"][0]
        insights.append(
            f"{item['date']} {item['source']}의 '{item['title']}'을 확인된 이슈 근거로 사용합니다."
        )

    return {
        "source": "BQ deterministic evidence",
        "tool": "bq_analysis",
        "summary_text": " ".join(insights),
        "render_data": {
            "contract_id": "E2",
            "calculation": "cross_source_causal_context",
            "insights": insights,
            "causal_posture": "temporal_overlap_not_causation",
            "never_aggregate_sources": True,
            "source_labels": list(market) + [source for source in ("CSD", "HIRA", "NEWS") if any(row["source"] == source for row in ledger)],
            "news_refs": news["refs"],
            "evidence_ledger": ledger,
            "chart_payloads": activity["charts"],
        },
    }


def _market_evidence(calls: list[Call]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for call in calls:
        data = _data(call)
        source_key = str((data.get("query_spec") or {}).get("source") or "")
        if source_key not in {"ubist", "iqvia_nsa"}:
            continue
        rows = _rows(data.get("brand_value_series_10pt"))
        known = [row for row in rows if row.get("period") and _number(row.get("value_krw")) is not None]
        if not known:
            continue
        latest = known[-1]
        label = "IQVIA NSA" if source_key == "iqvia_nsa" else "UBIST"
        evidence[label] = {
            "source": label,
            "kind": "number",
            "identity": f"{label}:{latest['period']}:brand_sales",
            "period": str(latest["period"]),
            "value_krw": _number(latest["value_krw"]),
            "references": [
                f"{label}.brand_value_series",
                f"{label}.render_data.brand_value_series_10pt",
            ],
        }
    return evidence


def _activity_evidence(calls: list[Call], market: dict[str, Any] | None) -> dict[str, Any]:
    call = next((item for item in calls if item.get("tool") == "csd_activity_trend"), None)
    rows = _rows(_data(call).get("series"))
    known = [row for row in rows if row.get("period") and _number(row.get("product_details")) is not None]
    if len(known) < 2:
        return {"insight": "", "ledger": [], "charts": []}
    first, last = known[0], known[-1]
    ledger = [
        {
            "source": "CSD",
            "kind": "time_window",
            "identity": f"CSD:{row['period']}:product_details",
            "period": str(row["period"]),
            "value": _number(row["product_details"]),
            "references": ["CSD.render_data.series"],
        }
        for row in (first, last)
    ]
    insight = (
        f"{first['period']}~{last['period']} CSD 활동 변화와 UBIST 매출 변화 시점이 겹칩니다. "
        "이는 시점 대조이며 인과를 단정하지 않습니다."
        if market is not None and first["period"] <= market["period"] <= last["period"]
        else ""
    )
    chart = {
        "chart_type": "line",
        "title": "CSD 활동 추이",
        "source": "CSD",
        "scope": "MARKET",
        "unit": "건",
        "evidence_refs": ["CSD.render_data.series"],
        "labels": [str(row["period"]) for row in known],
        "datasets": [{"label": "CSD 활동(건)", "unit": "건", "data": [_number(row["product_details"]) for row in known]}],
    }
    return {"insight": insight, "ledger": ledger, "charts": [chart]}


def _patient_evidence(calls: list[Call]) -> dict[str, Any]:
    disease = next((call for call in calls if call.get("tool") == "get_disease_stats"), None)
    inner = _data(disease).get("calls")
    candidates = inner if isinstance(inner, list) else []
    for call in candidates:
        for item in _rows(_data(call).get("items")):
            count = _number(item.get("ptntCnt"))
            period = str(item.get("year") or "").strip()
            if count is None or not period:
                continue
            evidence = {"source": "HIRA", "kind": "number", "identity": f"HIRA:{period}:patients", "period": period, "value": count}
            return {"insight": f"{period} HIRA 환자수는 {count:,.0f}명입니다.", "ledger": [evidence]}
    return {"insight": "", "ledger": []}


def _news_evidence(calls: list[Call]) -> dict[str, Any]:
    refs: list[dict[str, str]] = []
    ledger: list[dict[str, Any]] = []
    for call in calls:
        for item in _rows(_data(call).get("items")):
            ref = {key: str(item.get(key) or "").strip() for key in ("title", "date", "source", "url")}
            if not all(ref.values()):
                continue
            refs.append(ref)
            ledger.append({"source": "NEWS", "kind": "news", "identity": ref["url"], "period": ref["date"]})
    return {"refs": refs, "ledger": ledger}


def _data(call: Call | None) -> dict[str, Any]:
    value = call.get("render_data") if isinstance(call, dict) else None
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))
