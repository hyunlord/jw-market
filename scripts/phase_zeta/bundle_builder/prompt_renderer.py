from __future__ import annotations


def _krw_to_eok(value):
    try:
        number = float(value)
    except Exception:
        return "-"
    return f"{number / 100_000_000:.1f}억"


def _metric_value_text(point):
    if isinstance(point, dict):
        value = point.get("raw_value")
        if value is not None:
            return _krw_to_eok(value)
        if point.get("ms") is not None:
            return f"MS {point['ms']:.2f}%"
    if isinstance(point, (int, float)):
        return _krw_to_eok(point)
    return "-"


def render_narrative(
    bundle: dict,
    stage: str = "phenomenon",
) -> str:
    if stage not in {"phenomenon", "cause", "prediction", "recommendation", "all"}:
        raise ValueError(f"unsupported stage: {stage}")

    brand = bundle["brand_context"]["name"]
    lines = [
        f"# Phase ζ Bundle Narrative: {brand}",
        "",
        "## 브랜드 정보",
        f"- 영문명: {bundle['brand_context'].get('english_name') or '-'}",
        f"- 회사: {bundle['brand_context'].get('company') or '-'}",
        f"- 경쟁사: {', '.join(bundle['brand_context'].get('competitors') or []) or '-'}",
        "",
        "## 최근 시장 지표",
    ]

    brand_metrics = bundle["market_context"]["brand_metrics"]
    for metric_name, metric in brand_metrics.items():
        history = metric.get("history") or {}
        if not history:
            continue
        lines.append(f"### {metric_name}")
        lines.append("| 월 | 값 | 순위 | 점유율 |")
        lines.append("|---|---:|---:|---:|")
        for month, point in history.items():
            rank = point.get("rank", "-") if isinstance(point, dict) else "-"
            ms = f"{point.get('ms'):.2f}%" if isinstance(point, dict) and point.get("ms") is not None else "-"
            lines.append(f"| {month} | {_metric_value_text(point)} | {rank} | {ms} |")
        lines.append("")

    lines.extend(["## 최근 주요 이슈"])
    for event in bundle["event_bundle"]["direct_events"][:10]:
        lines.append(f"- [{event['published_date']}] ({event['score']}) {event['title']} — {event['summary']}")
    if not bundle["event_bundle"]["direct_events"]:
        lines.append("- 최근 cutoff 내 직접 이벤트 없음")

    lines.extend(["", "## 경쟁사 동향"])
    for competitor in bundle["competitor_context"]["competitors"]:
        events = competitor.get("recent_high_score_events") or []
        headline = events[0]["title"] if events else "고점 이벤트 없음"
        lines.append(f"- {competitor['name']}: {headline}")

    return "\n".join(lines) + "\n"
