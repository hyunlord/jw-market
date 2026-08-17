from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers, normalize_number
from jw_chat_agent_poc.orchestrator.external_passthrough_render import (
    normalize_external_section_headings,
)
from jw_chat_agent_poc.orchestrator.query_spec import CanonicalMetric, canonical_metrics_for_question
from jw_chat_agent_poc.service.answer_safety import (
    fact_token_allowed,
    strict_allowed_numbers,
    uploaded_file_fact_tokens,
)


_FAILED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "empty",
        "error",
        "failed",
        "failure",
        "hard_fail",
        "inapplicable",
        "missing",
        "no_data",
        "not_found",
        "query_failed",
        "semantic_empty",
        "timeout",
        "unavailable",
        "unsupported",
        "verification_failed",
    }
)
_TRUSTED_INTERNAL_SOURCES: Final[tuple[str, ...]] = (
    "UBIST",
    "IQVIA_NSA",
    "IQVIA NSA",
    "IQVIA_CSD",
    "IQVIA CSD",
)
_PROTECTED_METRIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bsales\b|\bmarket[_ ]?share\b|\brank\b|\bHHI\b|\bCR5\b|"
    r"\bgrowth[_ ]?rate\b|\bchannel[_ ]?share\b|매출|시장\s*점유율?|점유율?|"
    r"순위|성장률|채널\s*점유율?)",
    re.IGNORECASE,
)
_INTERNAL_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^#{1,6}\s*내부\s*정형\s*지표\s*$")
_WEB_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^#{1,6}\s*뉴스[·/]?외부\s*이슈\s*$")
_BLOCK_NOTICE: Final[str] = "근거 payload에 없는 수치는 출력에서 제외했습니다."
_DISPLAY_UNIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:억\s*원|억원|원|명|건|개|위|년|월|%p|%)$",
    re.IGNORECASE,
)


def enforce_numeric_copy_contract(
    question: str,
    answer: str,
    result: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Fail closed when final numeric tokens are absent from verified payload fields."""

    if result.get("file_only_ready"):
        return answer, {
            "contract": "numeric_copy_only_v1",
            "disposition": "pass",
            "blocked_line_count": 0,
            "blocked_tokens": [],
            "trusted_metric_blocked_count": 0,
            "verification_failed_fail_close": False,
            "question_has_numeric_token": bool(allowed_numbers(question)),
            "requested_metrics": [],
            "rendered_metrics": [],
            "dropped_metrics": [],
            "reason_codes": [],
        }

    verification_failed = _verification_failed(result)
    allowed_payload = _allowed_payload(result, derived_only=verification_failed)
    allowed = strict_allowed_numbers(allowed_payload, allowed_numbers(allowed_payload))
    trusted_payload = _trusted_internal_payload(result)
    trusted = strict_allowed_numbers(trusted_payload, allowed_numbers(trusted_payload))

    # Uploaded-file evidence can arrive as a FILE answer or as the file leg of a
    # MIXED answer. Admit only the retrieved file payload in either shape.
    #
    # This does not relax the market contract: market figures still need their
    # own backing, and a market number absent from the mart payload stays blocked.
    file_tokens = _uploaded_file_tokens(result)
    if file_tokens:
        allowed = tuple(sorted({*allowed, *file_tokens}))
        trusted = tuple(sorted({*trusted, *file_tokens}))

    kept: list[str] = []
    blocked_tokens: set[str] = set()
    blocked_line_count = 0
    trusted_metric_blocked_count = 0
    blocked_metrics: set[CanonicalMetric] = set()
    section = ""
    lines = answer.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _INTERNAL_SECTION_RE.fullmatch(stripped):
            section = "internal"
            kept.append(line)
            continue
        if _WEB_SECTION_RE.fullmatch(stripped):
            section = "web"
            kept.append(line)
            continue
        if _is_markdown_table_header(lines, index):
            kept.append(line)
            continue

        tokens = allowed_numbers(line)
        unsupported = tuple(token for token in tokens if not _copy_token_allowed(token, allowed))
        metric_line = bool(tokens and _PROTECTED_METRIC_RE.search(line))
        trusted_unsupported = tuple(
            token for token in tokens if not _copy_token_allowed(token, trusted)
        )
        web_metric_violation = section == "web" and metric_line
        trusted_metric_violation = metric_line and bool(trusted_unsupported)
        if unsupported or web_metric_violation or trusted_metric_violation:
            blocked_line_count += 1
            blocked_tokens.update(unsupported or trusted_unsupported or tokens)
            if web_metric_violation or trusted_metric_violation:
                trusted_metric_blocked_count += 1
            blocked_metrics.update(canonical_metrics_for_question(line))
            continue
        kept.append(line)

    cleaned = "\n".join(kept).strip()
    if blocked_line_count:
        cleaned = _insert_before_sources(cleaned, _blocked_numeric_notice(result))
    if not cleaned:
        cleaned = _blocked_numeric_notice(result)
    cleaned = normalize_external_section_headings(cleaned, result)
    requested_metrics = canonical_metrics_for_question(question)
    rendered_metrics = _rendered_metrics(cleaned, requested_metrics)
    dropped_metrics = tuple(metric for metric in requested_metrics if metric not in rendered_metrics)
    reason_codes = _metric_reason_codes(result, dropped_metrics, blocked_metrics)
    if dropped_metrics and "## 요청 지표 미제공" not in cleaned:
        cleaned = _insert_before_sources(cleaned, _requested_metric_missing_notice(dropped_metrics))
    report = {
        "contract": "numeric_copy_only_v1",
        "disposition": "blocked" if blocked_line_count else "pass",
        "blocked_line_count": blocked_line_count,
        "blocked_tokens": sorted(blocked_tokens),
        "trusted_metric_blocked_count": trusted_metric_blocked_count,
        "verification_failed_fail_close": verification_failed,
        "question_has_numeric_token": bool(allowed_numbers(question)),
        "requested_metrics": [metric.value for metric in requested_metrics],
        "rendered_metrics": [metric.value for metric in rendered_metrics],
        "dropped_metrics": [metric.value for metric in dropped_metrics],
        "reason_codes": list(reason_codes),
    }
    return cleaned, report


def _is_markdown_table_header(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    return (
        header.startswith("|")
        and header.endswith("|")
        and bool(re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", separator))
    )


def _rendered_metrics(
    answer: str,
    requested: tuple[CanonicalMetric, ...],
) -> tuple[CanonicalMetric, ...]:
    table_headers = "\n".join(
        line for line in answer.splitlines() if line.strip().startswith("|") and not re.search(r"\d", line)
    )
    rendered: list[CanonicalMetric] = []
    for metric in requested:
        labels = _metric_surface_labels(metric)
        in_header = any(label.casefold() in table_headers.casefold() for label in labels)
        numeric_claim = any(
            re.search(rf"{re.escape(label)}[^\n|]{{0,32}}[-+]?\d", answer, re.IGNORECASE)
            for label in labels
        )
        if in_header or numeric_claim:
            rendered.append(metric)
    return tuple(rendered)


def _metric_surface_labels(metric: CanonicalMetric) -> tuple[str, ...]:
    return {
        CanonicalMetric.GROWTH: ("성장률", "증감률", "YoY"),
        CanonicalMetric.SALES: ("매출",),
        CanonicalMetric.SHARE: ("점유율",),
        CanonicalMetric.RANK: ("순위",),
        CanonicalMetric.RANK_CHANGE: ("순위 변화", "순위변화", "순위 변동", "순위변동"),
        CanonicalMetric.PRESCRIPTION_VOLUME: ("처방량",),
        CanonicalMetric.PRESCRIPTION_COUNT: ("처방건수", "처방 건수"),
        CanonicalMetric.UNIT_PRICE: ("단가",),
        CanonicalMetric.CAGR: ("CAGR",),
        CanonicalMetric.HHI: ("HHI",),
        CanonicalMetric.MARKET_SIZE: ("시장 규모", "시장규모"),
    }[metric]


def _metric_reason_codes(
    result: Mapping[str, Any],
    dropped: tuple[CanonicalMetric, ...],
    blocked: set[CanonicalMetric],
) -> tuple[str, ...]:
    codes: list[str] = []
    if blocked.intersection(dropped):
        codes.append("numeric_copy_blocked")
    for metric in dropped:
        if metric in blocked:
            continue
        if metric is CanonicalMetric.UNIT_PRICE:
            code = "calculation_unavailable"
        elif metric is CanonicalMetric.RANK_CHANGE:
            code = "period_insufficient"
        elif not result.get("tool_calls"):
            code = "tool_not_run"
        else:
            code = "data_unavailable"
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def _requested_metric_missing_notice(metrics: tuple[CanonicalMetric, ...]) -> str:
    if metrics == (CanonicalMetric.GROWTH,):
        reason = "요청하신 성장률은 현재 근거에서 확인하지 못해 제외했습니다."
    elif metrics == (CanonicalMetric.UNIT_PRICE,):
        reason = "현재 근거에는 요청 지표의 정의·산식이 없어 산출할 수 없습니다."
    elif metrics == (CanonicalMetric.RANK_CHANGE,):
        reason = "요청 기간의 비교 기준 데이터가 부족해 순위 변화를 계산할 수 없습니다."
    else:
        labels = ", ".join(_metric_surface_labels(metric)[0] for metric in metrics)
        particle = "은" if _has_final_consonant(labels) else "는"
        reason = f"요청하신 {labels}{particle} 현재 근거에서 확인하지 못했습니다."
    return f"## 요청 지표 미제공\n\n{reason}"


def _has_final_consonant(value: str) -> bool:
    last = next((char for char in reversed(value.strip()) if char.isalpha()), "")
    codepoint = ord(last) if last else 0
    return 0xAC00 <= codepoint <= 0xD7A3 and (codepoint - 0xAC00) % 28 != 0


def _blocked_numeric_notice(result: Mapping[str, Any]) -> str:
    if _verification_failed(result):
        return "근거 검증에 실패해 수치를 제시하지 않습니다."
    calls = result.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            if not str(call.get("tool") or "").startswith("hira_disease_"):
                continue
            period = _hira_period(call)
            prefix = f"{period}년 " if period else ""
            if _call_failed(call):
                status = _call_status(call)
                if status == "no_data":
                    return f"{prefix}HIRA 조회 결과가 없어 환자수를 제시하지 않습니다."
                return f"{prefix}HIRA 조회에 실패해 환자수를 제시하지 않습니다."
    return _BLOCK_NOTICE


def _insert_before_sources(answer: str, notice: str) -> str:
    source_match = re.search(r"(?m)^##\s*출처\s*$", answer)
    if source_match is None:
        return f"{answer}\n\n{notice}".strip()
    before = answer[: source_match.start()].rstrip()
    after = answer[source_match.start() :].lstrip()
    return "\n\n".join(part for part in (before, notice, after) if part)


def _hira_period(call: Mapping[str, Any]) -> str:
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return ""
    period = str(render_data.get("requested_period") or "").strip()
    if period:
        return period.removesuffix("년")
    request = render_data.get("request")
    if not isinstance(request, Mapping):
        return ""
    return str(request.get("year") or "").strip().removesuffix("년")


def _call_status(call: Mapping[str, Any]) -> str:
    render_data = call.get("render_data")
    candidates = [
        call.get("status"),
        call.get("result_status"),
        call.get("semantic_status"),
    ]
    if isinstance(render_data, Mapping):
        candidates.append(render_data.get("status"))
    normalized = {
        str(candidate or "").strip().casefold()
        for candidate in candidates
    }
    return "no_data" if "no_data" in normalized else "failed"


def _copy_token_allowed(raw_token: str, payload_numbers: tuple[str, ...]) -> bool:
    if fact_token_allowed(raw_token, payload_numbers):
        return True
    token = _DISPLAY_UNIT_RE.sub("", normalize_number(str(raw_token))).strip().upper()
    if not token:
        return False
    return any(
        _DISPLAY_UNIT_RE.sub("", normalize_number(str(candidate))).strip().upper()
        == token
        for candidate in payload_numbers
    )


def _allowed_payload(result: Mapping[str, Any], *, derived_only: bool) -> str:
    payload: dict[str, Any] = {}
    control = result.get("_answer_control_layer")
    if isinstance(control, Mapping):
        derived = control.get("derived_payload")
        slot_status = control.get("slot_status")
        has_verified_slot = isinstance(slot_status, Mapping) and any(
            str(status).strip().casefold() == "verified"
            for status in slot_status.values()
        )
        if isinstance(derived, Mapping) and (not derived_only or has_verified_slot):
            payload["derived_payload"] = derived
    if derived_only:
        return _stable_text(payload)

    calls = result.get("tool_calls")
    if isinstance(calls, list):
        payload["tool_calls"] = [
            _numeric_call_payload(call)
            for call in calls
            if isinstance(call, Mapping) and not _call_failed(call)
        ]
    markdown = result.get("markdown_response")
    if isinstance(markdown, Mapping):
        payload["markdown_response"] = {
            key: markdown.get(key)
            for key in ("fact_md", "data_md", "evidence_md", "sources_md")
            if markdown.get(key) not in (None, "", [], {})
        }
    charts = result.get("charts")
    if isinstance(charts, list):
        payload["charts"] = charts
    marker = result.get("_external_passthrough")
    if isinstance(marker, Mapping):
        payload["external_passthrough"] = marker
    return _stable_text(payload)


def _uploaded_file_tokens(result: Mapping[str, Any]) -> tuple[str, ...]:
    """Numeric tokens actually retrieved from an uploaded-file evidence lane."""

    leg: Mapping[str, Any] | None = None
    mixed_leg = result.get("mixed_file_result")
    if isinstance(mixed_leg, Mapping):
        leg = mixed_leg
    elif str(result.get("context_scope") or "").strip().upper() == "FILE":
        leg = result
    elif result.get("file_context") or result.get("deterministic_file_answer"):
        # Legacy deterministic file results predate the explicit scope field.
        leg = result
    if leg is None:
        return ()
    sources = [
        str(leg.get("file_context") or ""),
        str(leg.get("deterministic_file_answer") or ""),
    ]
    combined = "\n".join(part for part in sources if part.strip())
    if not combined.strip():
        return ()
    return uploaded_file_fact_tokens(combined)


def _trusted_internal_payload(result: Mapping[str, Any]) -> str:
    calls = result.get("tool_calls")
    trusted_calls: list[dict[str, Any]] = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, Mapping) or _call_failed(call):
                continue
            render_data = call.get("render_data")
            source_text = " ".join(
                str(call.get(key) or "")
                for key in ("source", "source_name", "tool", "data_source")
            ).upper()
            if isinstance(render_data, Mapping):
                source_text = " ".join(
                    (
                        source_text,
                        str(render_data.get("source") or ""),
                        str(render_data.get("source_name") or ""),
                        str(render_data.get("data_source") or ""),
                    )
                ).upper()
            if any(source in source_text for source in _TRUSTED_INTERNAL_SOURCES):
                trusted_calls.append(_numeric_call_payload(call))
    control = result.get("_answer_control_layer")
    derived: Mapping[str, Any] | None = None
    if isinstance(control, Mapping) and isinstance(control.get("derived_payload"), Mapping):
        candidate = control["derived_payload"]
        source_names = candidate.get("source_names")
        source_text = _stable_text(source_names).upper()
        if any(source in source_text for source in _TRUSTED_INTERNAL_SOURCES):
            derived = candidate
    return _stable_text({"tool_calls": trusted_calls, "derived_payload": derived or {}})


def _numeric_call_payload(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: call.get(key)
        for key in (
            "tool",
            "source",
            "status",
            "summary_text",
            "render_data",
            "safe_url",
            "queried_at_utc",
            "result_status",
            "semantic_status",
        )
        if call.get(key) not in (None, "", [], {})
    }


def _call_failed(call: Mapping[str, Any]) -> bool:
    statuses = {
        str(call.get("status") or "").strip().casefold(),
        str(call.get("result_status") or call.get("semantic_status") or "").strip().casefold(),
    }
    render_data = call.get("render_data")
    if isinstance(render_data, Mapping):
        statuses.add(str(render_data.get("status") or "").strip().casefold())
        if str(render_data.get("error_code") or "").strip():
            return True
    return bool(statuses & _FAILED_STATUSES)


def _verification_failed(result: Mapping[str, Any]) -> bool:
    metrics = result.get("agent_loop_metrics")
    if isinstance(metrics, Mapping):
        if str(metrics.get("status") or "").strip().casefold() == "verification_failed":
            return True
    decomposition = result.get("decomposition")
    if isinstance(decomposition, Sequence) and not isinstance(decomposition, (str, bytes)):
        return any(
            isinstance(item, Mapping)
            and str(item.get("status") or "").strip().casefold() == "verification_failed"
            for item in decomposition
        )
    return False


def _stable_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
