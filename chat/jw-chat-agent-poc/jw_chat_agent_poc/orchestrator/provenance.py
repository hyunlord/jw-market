from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    allowed_numbers,
    cell,
    eok_value,
    items,
    latest_series_eok,
    number_value,
    pct_value,
    rank_value,
    source_description,
    source_label,
    table,
)
from jw_chat_agent_poc.orchestrator.surface_policy import can_surface_derived_value, cagr_operands_from_data, surface_year

LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    fact_id: str
    label: str
    value: str
    source: str
    tool: str
    path: str
    period: str
    allowed_numbers: tuple[str, ...]
    visible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NumberVerification:
    status: str
    unexpected_numbers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evidence_from_calls(calls: list[dict[str, Any]], data_md: str) -> tuple[EvidenceFact, ...]:
    facts: list[EvidenceFact] = []
    for call in calls:
        facts.extend(_structured_facts(call, len(facts)))
    for call in calls:
        facts.extend(_summary_text_facts(call, len(facts)))
    facts.extend(_table_token_facts(data_md, facts, len(facts)))
    return tuple(facts)


def evidence_markdown(facts: tuple[EvidenceFact, ...]) -> str:
    grouped: dict[str, set[str]] = {}
    for fact in facts:
        if not fact.visible:
            continue
        grouped.setdefault(fact.source, set()).add(fact.label)
    rows = tuple(
        (source, _source_provides(source), ", ".join(sorted(labels)))
        for source, labels in sorted(grouped.items())
    )
    if not rows:
        return ""
    return table("## 근거", ("출처", "제공 내용", "주요 항목"), rows)


def verify_markdown_numbers(markdown: str, facts: tuple[EvidenceFact, ...]) -> NumberVerification:
    allowed = {token for fact in facts for token in fact.allowed_numbers}
    unexpected = tuple(sorted(token for token in number_tokens(markdown) if token not in allowed))
    return NumberVerification(status="pass" if not unexpected else "fail", unexpected_numbers=unexpected)


def number_tokens(markdown: str) -> tuple[str, ...]:
    return allowed_numbers(LINK_TARGET_RE.sub("]", markdown))


def interpretation_has_unverified_numbers(markdown: str, allowed: tuple[str, ...]) -> bool:
    allowed_set = set(allowed)
    return any(token not in allowed_set for token in number_tokens(markdown))


def verification_notice() -> str:
    return "숫자 검증: 근거 표에 없는 숫자 표현을 감지해 해석을 확정 데이터 기준으로 제한했습니다."


def _structured_facts(call: dict[str, Any], offset: int) -> list[EvidenceFact]:
    data = call.get("render_data")
    if not isinstance(data, dict):
        return []
    tool = str(call.get("tool") or "-")
    source = _fact_source(call, data)
    period = str(data.get("period") or "")
    values = _metric_values(data)
    values.extend(_hira_values(data))
    facts: list[EvidenceFact] = []
    for label, value, path in values:
        if value:
            facts.append(
                _fact(
                    offset + len(facts),
                    label=label,
                    value=value,
                    source=source,
                    tool=tool,
                    path=path,
                    period=period,
                    visible=True,
                )
            )
    return facts


def _metric_values(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    period = data.get("period")
    if isinstance(period, str) and period:
        rows.append(("기간", period, "render_data.period"))
    rows.extend(
        (
            ("매출", eok_value(data.get("sales_억원"), data.get("sales_krw")), "render_data.sales_krw"),
            (
                "시장규모",
                eok_value(data.get("market_size_억원"), data.get("market_size_recent_krw")) or _latest_market_size(data),
                "render_data.market_size_recent_krw",
            ),
            (
                "필터 시장규모",
                eok_value(data.get("market_size_억원"), data.get("market_size_filtered_krw")),
                "render_data.market_size_filtered_krw",
            ),
            ("시장점유율", pct_value(data.get("ms_recent_pct", data.get("market_share"))), "render_data.ms_recent_pct"),
            ("순위", rank_value(data.get("rank"), data.get("total_brands_in_market")), "render_data.rank"),
            ("브랜드 CAGR", _surfaceable_cagr_value(data, "brand_cagr_5y_pct"), "render_data.brand_cagr_5y_pct"),
            ("시장 CAGR", _surfaceable_cagr_value(data, "market_cagr_5y_pct"), "render_data.market_cagr_5y_pct"),
            ("Excess growth", _surfaceable_cagr_value(data, "excess_growth_pct"), "render_data.excess_growth_pct"),
            ("HHI", number_value(data.get("hhi_recent", data.get("hhi"))), "render_data.hhi_recent"),
            ("Momentum", number_value(data.get("momentum_score")), "render_data.momentum_score"),
            ("EI", number_value(data.get("ei")), "render_data.ei"),
            ("기준 점유율", pct_value(data.get("from_ms_pct")), "render_data.from_ms_pct"),
            ("비교 점유율", pct_value(data.get("to_ms_pct")), "render_data.to_ms_pct"),
            ("점유율 변화", pct_value(data.get("ms_delta_pct")), "render_data.ms_delta_pct"),
            ("기준 매출", eok_value(data.get("from_sales_억원"), data.get("from_sales_krw")), "render_data.from_sales_krw"),
            ("비교 매출", eok_value(data.get("to_sales_억원"), data.get("to_sales_krw")), "render_data.to_sales_krw"),
            ("매출 변화", eok_value(data.get("sales_delta_억원"), data.get("sales_delta_krw")), "render_data.sales_delta_krw"),
            ("매출 변화율", pct_value(data.get("sales_delta_pct")), "render_data.sales_delta_pct"),
        )
    )
    return rows


def _surfaceable_cagr_value(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if can_surface_derived_value(value, cagr_operands=cagr_operands_from_data(data, key)):
        return pct_value(value)
    return ""


def _hira_values(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    rows: list[tuple[str, str, str]] = []
    for call_index, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        for item_index, item in enumerate(items(render_data)):
            count = item.get("ptntCnt")
            year = surface_year(render_data, item)
            if can_surface_derived_value(count, required_period=year):
                rows.append(
                    (
                        "환자수",
                        f"{year}년 {count}",
                        f"render_data.calls[{call_index}].render_data.items[{item_index}].ptntCnt",
                    )
                )
            code = item.get("sickCd")
            if code:
                rows.append(("질병코드", str(code), f"render_data.calls[{call_index}].render_data.items[{item_index}].sickCd"))
    return rows


def _latest_market_size(data: dict[str, Any]) -> str:
    market_series = data.get("market_size_series")
    if isinstance(market_series, list) and market_series:
        latest = market_series[-1]
        if isinstance(latest, dict):
            return eok_value(latest.get("value_억원"), latest.get("value_krw"))
    return latest_series_eok(data.get("series"))


def _summary_text_facts(call: dict[str, Any], offset: int) -> list[EvidenceFact]:
    summary = call.get("summary_text")
    if not isinstance(summary, str):
        return []
    tokens = number_tokens(summary)
    if not tokens:
        return []
    return [
        _fact(
            offset + index,
            label="도구 요약 숫자",
            value=token,
            source=_fact_source(call, {}),
            tool=str(call.get("tool") or "-"),
            path="summary_text",
            period="",
            visible=False,
        )
        for index, token in enumerate(tokens)
    ]


def _table_token_facts(data_md: str, facts: list[EvidenceFact], offset: int) -> list[EvidenceFact]:
    known = {token for fact in facts for token in fact.allowed_numbers}
    missing = [token for token in number_tokens(data_md) if token not in known]
    return [
        _fact(
            offset + index,
            label="표 숫자",
            value=token,
            source="렌더링된 도구 표",
            tool="markdown_table",
            path="data_md",
            period="",
            visible=False,
        )
        for index, token in enumerate(missing)
    ]


def _fact(
    index: int,
    *,
    label: str,
    value: str,
    source: str,
    tool: str,
    path: str,
    period: str,
    visible: bool,
) -> EvidenceFact:
    allowed = set(number_tokens(value))
    allowed.update(_period_display_tokens(value))
    if label == "환자수" and value.isdigit():
        allowed.update(number_tokens(f"{value}명"))
    if label in {"매출 변화", "매출 변화율", "점유율 변화"} and value and not value.startswith(("+", "-")):
        allowed.update(number_tokens(f"+{value}"))
    if label in {"매출 변화", "매출 변화율", "점유율 변화"} and value.startswith("-"):
        allowed.update(number_tokens(value[1:]))
    if label == "점유율 변화" and value.endswith("%"):
        allowed.update(number_tokens(f"{value}p"))
        if value.startswith("-"):
            allowed.update(number_tokens(f"{value[1:]}p"))
        elif not value.startswith("+"):
            allowed.update(number_tokens(f"+{value}p"))
    if label == "순위":
        allowed.update(_rank_display_tokens(value))
    return EvidenceFact(
        fact_id=f"fact_{_letters(index)}",
        label=cell(label),
        value=cell(value),
        source=cell(source),
        tool=cell(tool),
        path=cell(path),
        period=cell(period),
        allowed_numbers=tuple(sorted(allowed)),
        visible=visible,
    )


def _period_display_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for year, month in re.findall(r"(20\d{2})-(\d{2})", value):
        tokens.update(number_tokens(f"{year}년"))
        tokens.update(number_tokens(f"{int(month)}월"))
        tokens.update(number_tokens(f"{month}월"))
    return tokens


def _rank_display_tokens(value: str) -> set[str]:
    match = re.fullmatch(r"(\d+)/(\d+)", value)
    if not match:
        return set()
    rank, total = match.groups()
    tokens = set(number_tokens(f"{rank}위"))
    tokens.update(number_tokens(f"{total}개"))
    return tokens


def _letters(index: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    current = index
    chars: list[str] = []
    while True:
        current, remainder = divmod(current, len(alphabet))
        chars.append(alphabet[remainder])
        if current == 0:
            break
        current -= 1
    return "".join(reversed(chars))


def _fact_source(call: dict[str, Any], data: dict[str, Any]) -> str:
    data_source_label = data.get("source_label")
    if isinstance(data_source_label, str) and data_source_label:
        return source_label(data_source_label)
    source = call.get("source")
    return source_label(str(source or "tool_result"))


def _source_provides(source: str) -> str:
    return source_description(source)
