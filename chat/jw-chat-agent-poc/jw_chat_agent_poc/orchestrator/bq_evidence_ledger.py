from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


Call = dict[str, Any]


def finalize_bq_analysis_call(call: Call | None, calls: list[Call]) -> Call | None:
    if call is None:
        return None
    data = call.get("render_data")
    if not isinstance(data, dict):
        return call
    if not data.get("evidence_ledger"):
        data["evidence_ledger"] = build_evidence_ledger(calls)
    labels = _texts(data.get("source_labels"))
    if len(set(labels)) > 1:
        data["fusion_mode"] = "side_by_side"
    return call


def build_evidence_ledger(calls: list[Call]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for call in calls:
        data = _mapping(call.get("render_data"))
        source = _source(call, data)
        tool = str(call.get("tool") or "evidence")
        rows.extend(_series_rows(source, tool, data))
        rows.extend(_tabular_rows(source, tool, data))
        rows.extend(_item_rows(source, tool, data))
    return _deduplicate(rows)


def _series_rows(source: str, tool: str, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value_key in (
        ("brand_value_series_10pt", "value_krw"),
        ("market_size_series", "value_krw"),
        ("series", "product_details"),
    ):
        for item in _mappings(data.get(key)):
            period = str(item.get("period") or "").strip()
            value = item.get(value_key)
            if not period or value is None:
                continue
            rows.append(
                {
                    "source": source,
                    "kind": "series",
                    "identity": f"{tool}:{key}:{period}",
                    "period": period,
                    "value": value,
                    "references": _series_references(source, key),
                }
            )
    for item in _mappings(data.get("seller_series")):
        period = str(item.get("period") or "").strip()
        company = str(item.get("company") or "").strip()
        value = item.get("product_details")
        if not period or not company or value is None:
            continue
        rows.append(
            {
                "source": source,
                "kind": "series",
                "identity": f"{tool}:seller_series:{period}:{company}",
                "subject": company,
                "period": period,
                "value": value,
                "references": [f"{source}.render_data.seller_series"],
            }
        )
    return rows


def _tabular_rows(source: str, tool: str, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    dimension = str(data.get("requested_dimension") or "").strip()
    for item in _mappings(data.get("level_segments")):
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        references = [f"{source}.level_segments"]
        identity_parts = [tool]
        if dimension:
            references.append(f"{source}.{dimension}.level_segments")
            identity_parts.append(dimension)
        identity_parts.extend(("level_segments", name))
        row = {
            "source": source,
            "kind": "segment",
            "identity": ":".join(identity_parts),
            "value": value,
            "references": references,
        }
        if item.get("rank") is not None:
            row["rank"] = item["rank"]
        rows.append(row)

    trend_reference = f"{source}.level_top5_trend_series"
    for item in _mappings(data.get("level_top5_trend_series")):
        brand = str(item.get("brand") or "").strip()
        values = {
            key: item.get(key)
            for key in ("from_ms_pct", "to_ms_pct", "share_delta_pctp", "value_delta_krw")
            if item.get(key) is not None
        }
        if not brand or not values:
            continue
        rows.append(
            {
                "source": source,
                "kind": "trend",
                "identity": f"{tool}:level_top5_trend_series:{brand}",
                "subject": brand,
                "values": values,
                "references": [trend_reference],
            }
        )
    return rows


def _item_rows(source: str, tool: str, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    request = _mapping(data.get("request"))
    request_period = str(request.get("year") or "").strip()
    for item in _mappings(data.get("items")):
        patient_count = item.get("ptntCnt")
        patient_period = str(item.get("year") or item.get("yyyy") or request_period).strip()
        if patient_count is not None and patient_period:
            rows.append(
                {
                    "source": source,
                    "kind": "number",
                    "identity": f"{tool}:items:ptntCnt:{patient_period}",
                    "period": patient_period,
                    "value": patient_count,
                    "references": [f"{source}.render_data.items.ptntCnt"],
                }
            )
            continue
        identity = str(item.get("url") or item.get("title") or item.get("year") or "").strip()
        if identity:
            rows.append(
                {
                    "source": source,
                    "kind": "item",
                    "identity": f"{tool}:{identity}",
                    "period": str(item.get("date") or item.get("year") or "").strip() or None,
                }
            )
    for child in _mappings(data.get("calls")):
        rows.extend(_item_rows(source, tool, _mapping(child.get("render_data"))))
    return rows


def _series_references(source: str, key: str) -> list[str]:
    references = [f"{source}.{key}", f"{source}.render_data.{key}"]
    if key == "brand_value_series_10pt":
        references.append(f"{source}.brand_value_series")
    return references


def _source(call: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    spec = _mapping(data.get("query_spec"))
    value = str(spec.get("source") or call.get("source") or call.get("tool") or "UNKNOWN")
    normalized = value.casefold().replace("_", " ")
    if "iqvia" in normalized:
        return "IQVIA NSA"
    if "ubist" in normalized:
        return "UBIST"
    if "csd" in normalized:
        return "CSD"
    if "hira" in normalized or "disease" in normalized:
        return "HIRA"
    if "news" in normalized or "tavily" in normalized or "web" in normalized:
        return "NEWS"
    if "file" in normalized or "업로드" in normalized:
        return "FILE"
    return value.strip() or "UNKNOWN"


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (str(row["source"]), str(row["kind"]), str(row["identity"]))
        if key not in seen:
            seen.add(key)
            output.append(row)
    return output


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _texts(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
