from __future__ import annotations

import re


def _krw_to_eok(value):
    try:
        number = float(value)
    except Exception:
        return "-"
    return f"{number / 100_000_000:.1f}억"


def _number(value, decimals=2):
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "-"


def format_percent(value, kind="change"):
    if value is None:
        return "N/A"
    try:
        number = float(value)
    except Exception:
        return "N/A"
    if kind == "ratio":
        return f"{number:.2f}%"
    sign = "+" if number >= 0 else ""
    return f"{sign}{number:.2f}%"


def _percent(value, kind="change"):
    formatted = format_percent(value, kind=kind)
    return "-" if formatted == "N/A" else formatted


def _clean_article_text(text):
    return re.sub(r"(\d+(?:\.\d+)?)\s*억", r"\1 KRW-unit", text or "")


def _render_v1_1(bundle: dict) -> str:
    brand = bundle["brand_context"]["name"]
    lines = [
        f"# Phase ζ Bundle Narrative: {brand}",
        "",
        "## 1. 브랜드 정보",
        f"- 영문명: {bundle['brand_context'].get('english_name') or '-'}",
        f"- 회사: {bundle['brand_context'].get('company') or '-'}",
        f"- ML/CD: {bundle['brand_context'].get('ml_id') or '-'} / {bundle['brand_context'].get('cd_id') or '-'}",
        f"- ATC4: {bundle['brand_context'].get('atc4_code') or '-'}",
        f"- 사용 가능 source: {', '.join(bundle['brand_context'].get('available_sources') or []) or '-'}",
        "",
        f"## 2. 시장 view 분석 ({len(bundle['market_views'])} views)",
    ]

    for idx, view in enumerate(bundle["market_views"], start=1):
        lines.extend(
            [
                "",
                f"### 2.{idx} {view['view_id']}",
                "",
                "#### 시장 전체",
                "| 월 | 시장 규모 | HHI |",
                "|---|---:|---:|",
            ]
        )
        history = view["market_size"].get("history") or {}
        hhi = view["market_size"].get("hhi_5y") or {}
        for month in history:
            lines.append(f"| {month} | {_number(history.get(month))} {view['market_meta'].get('unit_label') or ''} | {_number(hhi.get(month))} |")

        lines.extend(
            [
                "",
                f"#### 선택 brand ({brand}) 지표",
                "| 월 | Raw value | M/S | 순위 | MoM | YoY | MAT YoY |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        target_history = view["target_brand_metric"].get("history") or {}
        for month, point in target_history.items():
            lines.append(
                "| "
                f"{month} | {_number(point.get('raw_value'))} | {_percent(point.get('ms_pct'), kind='ratio')} | "
                f"{point.get('rank') or '-'} | {_percent(point.get('mom_pct'), kind='change')} | "
                f"{_percent(point.get('yoy_pct'), kind='change')} | {_percent(point.get('mat_yoy_pct'), kind='change')} |"
            )
        mat = view["target_brand_metric"].get("mat_12m_absolute") or {}
        if mat.get("latest_period"):
            lines.append("")
            lines.append(f"MAT 12개월 절대값: {_number(mat.get('value'))} {view['market_meta'].get('unit_label') or ''}")

        extras = view["target_brand_metric"].get("kpi_extras") or {}
        lines.extend(
            [
                "",
                "KPI 부가:",
                f"- EI: {_number(extras.get('ei'))} (basis: {extras.get('ei_basis') or '-'})",
                f"- Brand CAGR 5y: {_percent(extras.get('brand_cagr_5y_pct'), kind='change')} / Market CAGR 5y: {_percent(extras.get('market_cagr_5y_pct'), kind='change')}",
                f"- Momentum: {_number(extras.get('momentum_score'), 4)}",
                "",
                "#### 시장 상위 5 (선택 brand 제외)",
                "| 순위 | Brand | Raw value | M/S | EI | CAGR 5y | Momentum |",
                "|---:|---|---:|---:|---:|---:|---:|",
            ]
        )
        for comp in view.get("competitors_top5", []):
            latest_point = next(iter((comp.get("history") or {}).values()), {})
            comp_extra = comp.get("kpi_extras") or {}
            lines.append(
                "| "
                f"{comp.get('rank_in_market') or '-'} | {comp['brand_name']} | {_number(latest_point.get('raw_value'))} | "
                f"{_percent(latest_point.get('ms_pct'), kind='ratio')} | {_number(comp_extra.get('ei'))} | "
                f"{_percent(comp_extra.get('brand_cagr_5y_pct'), kind='change')} | {_number(comp_extra.get('momentum_score'), 4)} |"
            )

        rows = view.get("channel_breakdown", {}).get("top5_in_channel") or []
        lines.extend(["", f"#### \"{view.get('channel_breakdown', {}).get('channel', '전체')}\" 채널 분포"])
        if rows:
            lines.append("| 순위 | Brand | Raw value | M/S |")
            lines.append("|---:|---|---:|---:|")
            for row in rows:
                suffix = " (target)" if row.get("is_target") else ""
                lines.append(
                    f"| {row.get('rank') or '-'} | {row.get('brand')}{suffix} | {_number(row.get('raw_value'))} | {_percent(row.get('ms_pct'), kind='ratio')} |"
                )
        else:
            lines.append("- 전체 채널 top5 정보 없음")

    events = bundle["event_bundle"]
    lines.extend(["", "## 3. 선택 brand 의 주요 이슈", "", f"### 3.1 Brand 직접 언급 ({len(events['events_brand_centric'])})"])
    for event in events["events_brand_centric"]:
        lines.append(
            f"- [{event['published_date']} score {event['score']} {event['tag']}] "
            f"{_clean_article_text(event['title'])} — {_clean_article_text(event['summary'])}"
        )
    if not events["events_brand_centric"]:
        lines.append("- 고점 이벤트 없음")

    lines.extend(["", f"### 3.2 시장 트렌드 ({len(events['events_market_trend'])})"])
    for event in events["events_market_trend"]:
        lines.append(
            f"- [{event['published_date']} score {event['score']} {event['tag']}] "
            f"{_clean_article_text(event['title'])} — {_clean_article_text(event['summary'])}"
        )
    if not events["events_market_trend"]:
        lines.append("- 고점 이벤트 없음")

    lines.extend(["", f"### 3.3 cross_match ({len(events['cross_match_events'])})"])
    for event in events["cross_match_events"]:
        mirrored = ", ".join(event.get("mirrored_from") or [])
        lines.append(f"- [{event['published_date']} score {event['score']}] {_clean_article_text(event['title'])} (mirrored from: {mirrored})")
    if not events["cross_match_events"]:
        lines.append("- cross_match 이벤트 없음")

    tag_parts = [f"{tag}: {count}" for tag, count in events["tag_distribution"].items() if count]
    lines.extend(["", "### 3.4 Tag 분포", f"- {', '.join(tag_parts) if tag_parts else '태그 없음'}"])

    lines.extend(["", "## 4. 시장 상위 경쟁사의 이슈"])
    competitor_events = bundle["competitor_events"]
    event_groups = competitor_events.get("by_view") or competitor_events.get("by_source") or {}
    for group_key, payload in event_groups.items():
        lines.append("")
        label = payload.get("view_id") or group_key
        lines.append(f"### {label} 시장 top5 의 events")
        for comp in payload.get("competitors", []):
            lines.append("")
            lines.append(f"#### {comp['brand_name']} (rank {comp.get('rank_in_market') or '-'})")
            if not comp.get("events"):
                lines.append("- 고점 이벤트 없음")
                continue
            for event in comp["events"]:
                lines.append(
                    f"- [{event['published_date']} score {event['score']} {event['tag']}] "
                    f"{_clean_article_text(event['title'])} — {_clean_article_text(event['summary'])}"
                )

    lines.extend(["", "## 5. 시계열 예측", "Phase 23+ 또는 Phase η 에서 활성화 예정."])
    return "\n".join(lines) + "\n"


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
    if "market_views" in bundle:
        return _render_v1_1(bundle)

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
