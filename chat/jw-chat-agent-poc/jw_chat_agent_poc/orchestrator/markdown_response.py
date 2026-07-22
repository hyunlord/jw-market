from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    allowed_numbers,
    cell,
    eok_value,
    latest_series_eok,
    number_value,
    pct_value,
    rank_value,
    sanitize_interpretation,
    table,
)
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.call_normalization import dedupe_blocked_metric_messages
from jw_chat_agent_poc.orchestrator.markdown_renderers import call_data_md
from jw_chat_agent_poc.orchestrator.market_insights import render_market_insights
from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_source_block
from jw_chat_agent_poc.orchestrator.provenance import (
    evidence_from_calls,
    evidence_markdown,
    verification_notice,
    verify_markdown_numbers,
)
from jw_chat_agent_poc.orchestrator.surface_policy import can_surface_derived_value, cagr_operands_from_data, surface_year


@dataclass(frozen=True, slots=True)
class MarkdownResponse:
    markdown: str
    summary_md: str
    interpretation_md: str
    data_md: str
    fact_md: str
    evidence_md: str
    sources_md: str
    notice_md: str
    allowed_numbers: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    verification: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarkdownResponseBuilder:
    def build(
        self,
        *,
        brand: str,
        calls: list[dict[str, Any]],
        sources: list[str],
        notices: list[str] | None = None,
    ) -> MarkdownResponse:
        calls = dedupe_blocked_metric_messages(calls)
        summary_md = self._summary_md(brand, sources)
        data_md = self._data_md(calls)
        facts = evidence_from_calls(calls, data_md)
        fact_md = answer_fact_markdown(calls, sources)
        evidence_md = evidence_markdown(facts)
        interpretation_md = self._interpretation_md(calls)
        sources_md = self._sources_md(calls, sources)
        notice_md = self._notice_md(notices or [])
        markdown = self._join(summary_md, interpretation_md, data_md, evidence_md, sources_md, notice_md)
        verification = verify_markdown_numbers(markdown, facts)
        if verification.status != "pass":
            interpretation_md = "## 해석\n\n- 표에 포함된 확정 데이터만 기준으로 해석합니다."
            notice_md = self._notice_md([*(notices or []), verification_notice()])
            markdown = self._join(summary_md, interpretation_md, data_md, evidence_md, sources_md, notice_md)
            verification = verify_markdown_numbers(markdown, facts)
        return MarkdownResponse(
            markdown=markdown,
            summary_md=summary_md,
            interpretation_md=interpretation_md,
            data_md=data_md,
            fact_md=fact_md,
            evidence_md=evidence_md,
            sources_md=sources_md,
            notice_md=notice_md,
            allowed_numbers=tuple(sorted({token for fact in facts for token in fact.allowed_numbers})),
            evidence=tuple(fact.to_dict() for fact in facts),
            verification=verification.to_dict(),
        )

    def no_data(self, message: str) -> MarkdownResponse:
        summary_md = ""
        interpretation_md = f"## 해석\n\n- {cell(message)}"
        sources_md = provenance_source_block([], ["none"])
        markdown = self._join(summary_md, interpretation_md, sources_md)
        return self._static_response(markdown, summary_md, interpretation_md, "", "", "", sources_md, "")

    def unsupported_brand(self, message: str) -> MarkdownResponse:
        summary_md = ""
        interpretation_md = f"## 해석\n\n- {cell(message)}"
        sources_md = provenance_source_block([], ["unsupported_brand"])
        markdown = self._join(summary_md, interpretation_md, sources_md)
        return self._static_response(markdown, summary_md, interpretation_md, "", "", "", sources_md, "")

    def ambiguous_brand(self, message: str) -> MarkdownResponse:
        summary_md = ""
        interpretation_md = f"## 해석\n\n- {cell(message)}"
        sources_md = provenance_source_block([], ["ambiguous_brand"])
        markdown = self._join(summary_md, interpretation_md, sources_md)
        return self._static_response(markdown, summary_md, interpretation_md, "", "", "", sources_md, "")

    def field_not_exposed(self, message: str) -> MarkdownResponse:
        summary_md = ""
        interpretation_md = f"## 해석\n\n- {cell(message)}"
        sources_md = provenance_source_block([], ["field_not_exposed"])
        markdown = self._join(summary_md, interpretation_md, sources_md)
        return self._static_response(markdown, summary_md, interpretation_md, "", "", "", sources_md, "")

    @staticmethod
    def _static_response(
        markdown: str,
        summary_md: str,
        interpretation_md: str,
        data_md: str,
        fact_md: str,
        evidence_md: str,
        sources_md: str,
        notice_md: str,
    ) -> MarkdownResponse:
        return MarkdownResponse(
            markdown=markdown,
            summary_md=summary_md,
            interpretation_md=interpretation_md,
            data_md=data_md,
            fact_md=fact_md,
            evidence_md=evidence_md,
            sources_md=sources_md,
            notice_md=notice_md,
            allowed_numbers=allowed_numbers(markdown),
            evidence=(),
            verification={"status": "pass", "unexpected_numbers": ()},
        )

    @staticmethod
    def sanitize_interpretation(markdown: str, numbers: tuple[str, ...]) -> str:
        return sanitize_interpretation(markdown, numbers)

    @staticmethod
    def _summary_md(brand: str, sources: list[str]) -> str:
        return ""

    @staticmethod
    def _interpretation_md(calls: list[dict[str, Any]]) -> str:
        bullets = [f"- {cell(line)}" for line in render_market_insights(calls)]
        primary_brand = MarkdownResponseBuilder._primary_insight_brand(calls)
        for call in calls:
            summary = MarkdownResponseBuilder._interpretation_summary(call)
            data = call.get("render_data")
            call_brand = str(data.get("brand") or "") if isinstance(data, dict) else ""
            if (
                bullets
                and primary_brand
                and call_brand == primary_brand
                and not MarkdownResponseBuilder._requires_interpretation_summary(call)
            ):
                continue
            if isinstance(summary, str) and summary and "None" not in summary:
                bullets.append(f"- {cell(summary)}")
        if not bullets:
            bullets.append("- 확인 가능한 도구 결과가 없어 정성 해석을 제한합니다.")
        return "## 해석\n\n" + "\n".join(bullets[:8])

    @staticmethod
    def _primary_insight_brand(calls: list[dict[str, Any]]) -> str:
        for call in calls:
            data = call.get("render_data")
            if isinstance(data, dict) and isinstance(data.get("series_insight"), dict):
                return str(data.get("brand") or "")
        return ""

    @staticmethod
    def _requires_interpretation_summary(call: dict[str, Any]) -> bool:
        data = call.get("render_data")
        status = data.get("status") if isinstance(data, dict) else None
        return call.get("tool") in {"query_failed", "unsupported_metric"} or status in {
            "error",
            "query_failed",
            "unsupported",
        }

    @staticmethod
    def _interpretation_summary(call: dict[str, Any]) -> str:
        tool = str(call.get("tool") or "")
        render_data = call.get("render_data")
        if tool in {"get_brand_metric", "get_market_landscape", "unsupported_metric", "query_failed", "agent_calculation"} and isinstance(render_data, dict):
            return MarkdownResponseBuilder._metric_interpretation_summary(render_data)
        if tool == "get_disease_stats" and isinstance(render_data, dict):
            return MarkdownResponseBuilder._hira_interpretation_summary(render_data)
        summary = call.get("summary_text")
        return summary if isinstance(summary, str) else ""

    @staticmethod
    def _metric_interpretation_summary(data: dict[str, Any]) -> str:
        if data.get("status") in {"error", "query_failed"}:
            return str(data.get("message") or "요청한 지표 조회 실행이 실패했습니다. 데이터 미보유로 해석하지 않습니다.")
        if data.get("status") == "unsupported":
            return str(data.get("message") or "요청한 지표는 현재 지원 범위 밖입니다.")

        subject = str(data.get("brand") or data.get("market_name") or "해당 시장")
        metric = data.get("metric")
        if metric == "largest_competitor_sales":
            sales = eok_value(data.get("sales_억원"), data.get("sales_krw"))
            return f"같은 시장에서 작년 제일 큰 경쟁사는 {subject}이며 매출 {sales}입니다."
        if metric == "market_share_delta":
            delta = pct_value(data.get("ms_delta_pct"))
            return f"{subject} 3달전 대비 점유율이 {delta} 변했습니다."
        if metric == "sales_delta":
            delta = eok_value(data.get("sales_delta_억원"), data.get("sales_delta_krw"))
            pct = pct_value(data.get("sales_delta_pct"))
            period = data.get("period")
            return f"{subject} 매출 변화는 {period} 기준 {delta}({pct})입니다."
        if metric == "series":
            return f"{subject} 매출 추이는 데이터 표와 계산 행에 정리했습니다."
        if not any(
            data.get(key) is not None
            for key in (
                "sales_억원",
                "sales_krw",
                "ms_recent_pct",
                "market_share",
                "rank",
                "market_size_억원",
                "market_size_recent_krw",
                "hhi_recent",
                "hhi",
                "momentum_score",
                "ei",
            )
        ):
            return str(data.get("message") or "표에 포함된 확정 지표만 기준으로 해석합니다.")
        view_label = data.get("view_label")
        if isinstance(view_label, str) and view_label:
            return f"{subject}의 {view_label} 기준 지표 수치는 데이터 표에서 한 번만 확인할 수 있습니다."
        return f"{subject}의 지표 수치는 데이터 표에서 한 번만 확인할 수 있습니다."

    @staticmethod
    def _hira_interpretation_summary(data: dict[str, Any]) -> str:
        stats = MarkdownResponseBuilder._first_hira_patient_count(data)
        if stats is None:
            return "HIRA 질병 통계에서 표시할 환자수 항목을 찾지 못했습니다."
        label, count, year = stats
        return f"HIRA 질병 통계는 {label} {year}년 환자수 {count}명을 기준으로 확인됩니다."

    @staticmethod
    def _first_hira_patient_count(data: dict[str, Any]) -> tuple[str, str, str] | None:
        calls = data.get("calls")
        if not isinstance(calls, list):
            return None
        for call in calls:
            if not isinstance(call, dict):
                continue
            render_data = call.get("render_data")
            if not isinstance(render_data, dict):
                continue
            raw_items = render_data.get("items")
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                count = item.get("ptntCnt")
                year = surface_year(render_data, item)
                if not can_surface_derived_value(count, required_period=year):
                    continue
                label = str(item.get("inpatOpat") or item.get("age") or item.get("grade") or item.get("lcName") or item.get("sickNm") or "대표")
                return label, str(count), year
        return None

    @staticmethod
    def _latest_market_series_value(data: dict[str, Any]) -> str:
        raw_series = data.get("market_size_series")
        if isinstance(raw_series, list) and raw_series:
            latest = raw_series[-1]
            if isinstance(latest, dict):
                return eok_value(latest.get("value_억원"), latest.get("value_krw"))
        return latest_series_eok(data.get("series"))

    @staticmethod
    def _two_decimal_value(value: Any) -> str:
        if isinstance(value, int | float):
            return f"{float(value):,.2f}"
        return number_value(value)

    @staticmethod
    def _data_md(calls: list[dict[str, Any]]) -> str:
        seen: set[tuple[str, str, str, str]] = set()
        seen_insights: set[tuple[str, str, str, str]] = set()
        seen_level_segments: set[str] = set()
        seen_metric_labels: set[str] = set()
        blocks: list[str] = []
        semantic_metric_headers = any(
            isinstance(call.get("render_data"), dict)
            and isinstance(call["render_data"].get("series_insight"), dict)
            for call in calls
        )
        for call in calls:
            if call.get("tool") == "matching_policy_notice":
                continue
            insight_signature = MarkdownResponseBuilder._insight_signature(call)
            if insight_signature is not None:
                if insight_signature in seen_insights:
                    continue
                seen_insights.add(insight_signature)
            signature = MarkdownResponseBuilder._data_signature(call)
            if signature in seen:
                continue
            seen.add(signature)
            render_call = MarkdownResponseBuilder._call_without_duplicate_metric_fields(call, seen_metric_labels)
            render_call = MarkdownResponseBuilder._call_without_duplicate_level_segments(
                render_call,
                seen_level_segments,
            )
            if semantic_metric_headers:
                render_call = MarkdownResponseBuilder._call_with_semantic_metric_header(render_call)
            block = call_data_md(render_call)
            if block:
                blocks.append(block)
                seen_metric_labels.update(MarkdownResponseBuilder._visible_metric_labels(render_call))
                level_signature = MarkdownResponseBuilder._level_segments_signature(render_call)
                if level_signature is not None:
                    seen_level_segments.add(level_signature)
        if not blocks:
            return "## 데이터\n\n- 표시할 표 데이터가 없습니다."
        return "## 데이터\n\n" + "\n\n".join(blocks)

    @staticmethod
    def _call_with_semantic_metric_header(call: dict[str, Any]) -> dict[str, Any]:
        if call.get("tool") not in {
            "get_brand_metric",
            "get_market_landscape",
            "unsupported_metric",
            "query_failed",
            "agent_calculation",
        }:
            return call
        data = call.get("render_data")
        if not isinstance(data, dict):
            return call
        clean_call = dict(call)
        clean_call["render_data"] = {**data, "_semantic_value_header": True}
        return clean_call

    @staticmethod
    def _insight_signature(call: dict[str, Any]) -> tuple[str, str, str, str] | None:
        data = call.get("render_data")
        if not isinstance(data, dict) or not isinstance(data.get("series_insight"), dict):
            return None
        return (
            str(data.get("brand") or ""),
            str(data.get("market_id") or ""),
            str(data.get("source_label") or ""),
            str(data.get("period") or ""),
        )

    @staticmethod
    def _call_without_duplicate_metric_fields(call: dict[str, Any], seen_labels: set[str]) -> dict[str, Any]:
        if call.get("tool") not in {"get_brand_metric", "get_market_landscape", "unsupported_metric", "query_failed", "agent_calculation"}:
            return call
        data = call.get("render_data")
        if not isinstance(data, dict) or not seen_labels:
            return call
        clean_data = dict(data)
        for label, keys in MarkdownResponseBuilder._dedupe_metric_fields().items():
            if label not in seen_labels:
                continue
            for key in keys:
                clean_data.pop(key, None)
        clean_call = dict(call)
        clean_call["render_data"] = clean_data
        return clean_call

    @staticmethod
    def _call_without_duplicate_level_segments(
        call: dict[str, Any],
        seen: set[str],
    ) -> dict[str, Any]:
        signature = MarkdownResponseBuilder._level_segments_signature(call)
        if signature is None or signature not in seen:
            return call
        data = call.get("render_data")
        clean_data = dict(data) if isinstance(data, dict) else {}
        clean_data.pop("level_segments", None)
        clean_call = dict(call)
        clean_call["render_data"] = clean_data
        return clean_call

    @staticmethod
    def _level_segments_signature(call: dict[str, Any]) -> str | None:
        data = call.get("render_data")
        segments = data.get("level_segments") if isinstance(data, dict) else None
        if not isinstance(segments, list) or not segments:
            return None
        return json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _visible_metric_labels(call: dict[str, Any]) -> set[str]:
        if call.get("tool") not in {"get_brand_metric", "get_market_landscape", "unsupported_metric", "query_failed", "agent_calculation"}:
            return set()
        data = call.get("render_data")
        if not isinstance(data, dict):
            return set()
        labels: set[str] = set()
        if data.get("period") is not None:
            labels.add("기간")
        scalar_keys = {
            "매출": ("sales_억원", "sales_krw"),
            "시장점유율": ("ms_recent_pct", "market_share"),
            "순위": ("rank",),
            "시장규모": ("market_size_억원", "market_size_recent_krw"),
            "브랜드 CAGR": ("brand_cagr_5y_pct",),
            "시장 CAGR": ("market_cagr_5y_pct",),
            "Excess growth": ("excess_growth_pct",),
            "HHI": ("hhi_recent", "hhi"),
            "Momentum": ("momentum_score",),
            "EI": ("ei",),
            "기준 점유율": ("from_ms_pct",),
            "비교 점유율": ("to_ms_pct",),
            "점유율 변화": ("ms_delta_pct",),
            "기준 매출": ("from_sales_krw",),
            "비교 매출": ("to_sales_krw",),
            "매출 변화": ("sales_delta_krw",),
            "매출 변화율": ("sales_delta_pct",),
        }
        for label, keys in scalar_keys.items():
            if any(MarkdownResponseBuilder._metric_field_visible(data, key) for key in keys):
                labels.add(label)
        return labels

    @staticmethod
    def _metric_field_visible(data: dict[str, Any], key: str) -> bool:
        value = data.get(key)
        if value is None:
            return False
        if key in {"brand_cagr_5y_pct", "market_cagr_5y_pct", "excess_growth_pct"}:
            return can_surface_derived_value(value, cagr_operands=cagr_operands_from_data(data, key))
        return True

    @staticmethod
    def _dedupe_metric_fields() -> dict[str, tuple[str, ...]]:
        return {
            "기간": ("period",),
            "시장규모": ("market_size_억원", "market_size_recent_krw"),
            "시장 CAGR": ("market_cagr_5y_pct",),
            "HHI": ("hhi_recent", "hhi"),
            "Momentum": ("momentum_score",),
            "EI": ("ei",),
        }

    @staticmethod
    def _data_signature(call: dict[str, Any]) -> tuple[str, str, str, str]:
        data = call.get("render_data")
        if not isinstance(data, dict):
            return (str(call.get("tool") or ""), "", "", "")
        return (
            str(call.get("tool") or ""),
            str(data.get("brand") or data.get("market_id") or data.get("source_label") or ""),
            str(data.get("metric") or ""),
            str(data.get("period") or ""),
        )

    @staticmethod
    def _sources_md(calls: list[dict[str, Any]], sources: list[str]) -> str:
        return provenance_source_block(calls, sources)

    @staticmethod
    def _notice_md(notices: list[str]) -> str:
        clean = list(dict.fromkeys(notice for notice in notices if notice))
        if not clean:
            return ""
        return "## 주의\n" + "\n".join(f"- {cell(notice)}" for notice in clean)

    @staticmethod
    def _join(*parts: str) -> str:
        return "\n\n".join(part for part in parts if part)
