from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


_LINT_RE = re.compile(
    r"(?:해당|이)\s*주장은?\s*현재\s*근거\s*자격으로\s*확인되지\s*않았습니다[.!]?"
)
_PROGRESS_ONLY_RE = re.compile(
    r"(?:속한\s+.+?시장의\s+상위\s+브랜드|시장\s+상위\s+브랜드|"
    r"시장의\s+원인분석\s+분해\s+데이터|자료\s+수집)"
    r".*?(?:전략\s*mart|데이터마트|직접).*?(?:조회|수집)(?:했|되)",
    re.IGNORECASE,
)
_METRIC_SUMMARY_RE = re.compile(
    r"^(?P<subject>.+?)\s+(?P<year>20\d{2})-(?P<month>\d{2})\s+"
    r"(?P<source>UBIST|IQVIA(?:_NSA)?)\s+전략\s*mart\s*지표:\s*(?P<facts>.+)$",
    re.IGNORECASE,
)
_RAW_WON_RE = re.compile(
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?:\(\s*(?:원|KRW)\s*\)|원|KRW)",
    re.IGNORECASE,
)
_EOK_RE = re.compile(r"(?P<approx>약\s*)?(?P<value>\d[\d,]*(?:\.\d+)?)\s*억원")
_RX_RE = re.compile(
    r"(?P<approx>약\s*)?(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:\(\s*Rx\s*\)|Rx)(?![A-Za-z])",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(
    r"(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>\(\s*%(?P<paren_p>p)?\s*\)|%(?P<plain_p>p)?)",
    re.IGNORECASE,
)
_DELTA_RE = re.compile(
    r"(?P<start>-?\d[\d,]*(?:\.\d+)?)억원에서\s*"
    r"(?P<end>-?\d[\d,]*(?:\.\d+)?)억원으로\s*"
    r"(?P<delta>-?\d[\d,]*(?:\.\d+)?)억원\s*"
    r"(?P<direction>증가|감소)"
)


def normalize_answer_surface(text: str) -> tuple[str, dict[str, Any]]:
    """Render user-facing values without changing the evidence payload."""

    removed = 0
    rewritten = 0
    lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("## ") or stripped.startswith("|"):
            lines.append(raw_line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", stripped)
        kept: list[str] = []
        for sentence in sentences:
            cleaned = sentence.strip()
            if not cleaned:
                continue
            if _LINT_RE.fullmatch(cleaned) or _PROGRESS_ONLY_RE.search(cleaned):
                removed += 1
                continue
            metric = _rewrite_metric_summary(cleaned)
            rewritten += int(metric != cleaned)
            kept.append(_fix_subject_particle(metric))
        if kept:
            lines.append(" ".join(kept))

    cleaned_text = "\n".join(lines).strip()
    cleaned_text, raw_won = _replace_raw_won(cleaned_text)
    cleaned_text, eok = _replace_eok(cleaned_text)
    cleaned_text, rx = _replace_rx(cleaned_text)
    cleaned_text, percentages = _replace_percentages(cleaned_text)
    cleaned_text, deltas = _repair_displayed_deltas(cleaned_text)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()
    return cleaned_text, {
        "rounding": "ROUND_HALF_UP",
        "removed_sentences": removed,
        "rewritten_sentences": rewritten,
        "raw_won_values": raw_won,
        "eok_values": eok,
        "rx_values": rx,
        "percentage_values": percentages,
        "recomputed_deltas": deltas,
    }


def _rewrite_metric_summary(sentence: str) -> str:
    match = _METRIC_SUMMARY_RE.match(sentence.rstrip("."))
    if match is None:
        return sentence
    month = int(match.group("month"))
    facts = match.group("facts").rstrip(".")
    return (
        f"{match.group('subject')}의 {match.group('year')}년 {month}월 "
        f"{match.group('source')} 지표는 {facts}입니다."
    )


def _fix_subject_particle(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        subject = match.group("subject")
        code = ord(subject[-1]) - 0xAC00
        particle = "이" if 0 <= code <= 11171 and code % 28 else "가"
        return f"{subject}{particle} 속한"

    return re.sub(r"(?P<subject>[가-힣A-Za-z0-9]+)이\s+속한", replace, text)


def _replace_raw_won(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        value = _decimal(match.group("value"))
        if value is None or abs(value) < Decimal("10000000"):
            return match.group(0)
        if _hira_cost_context(text, match.start()):
            return match.group(0)
        count += 1
        market = _market_context(text, match.start())
        return _format_eok(value / Decimal("100000000"), market=market)

    return _RAW_WON_RE.sub(replace, text), count


def _replace_eok(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        value = _decimal(match.group("value"))
        if value is None:
            return match.group(0)
        count += 1
        market = _market_context(text, match.start())
        return _format_eok(value, market=market)

    return _EOK_RE.sub(replace, text), count


def _replace_rx(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        value = _decimal(match.group("value"))
        if value is None:
            return match.group(0)
        count += 1
        if abs(value) >= Decimal("10000"):
            ten_thousands = value / Decimal("10000")
            shown = ten_thousands.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            return f"약 {shown:,}만 Rx"
        return f"{_fixed(value, Decimal('0.01'))} Rx"

    return _RX_RE.sub(replace, text), count


def _replace_percentages(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        value = _decimal(match.group("value"))
        if value is None:
            return match.group(0)
        count += 1
        suffix = "%p" if match.group("paren_p") or match.group("plain_p") else "%"
        return f"{_fixed(value, Decimal('0.01'))}{suffix}"

    return _PERCENT_RE.sub(replace, text), count


def _repair_displayed_deltas(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        start = _decimal(match.group("start"))
        end = _decimal(match.group("end"))
        if start is None or end is None:
            return match.group(0)
        count += 1
        delta = abs(end - start).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return (
            f"{_fixed(start, Decimal('0.01'))}억원에서 "
            f"{_fixed(end, Decimal('0.01'))}억원으로 "
            f"{_fixed(delta, Decimal('0.01'))}억원 {match.group('direction')}"
        )

    return _DELTA_RE.sub(replace, text), count


def _format_eok(value: Decimal, *, market: bool) -> str:
    if market:
        rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return f"약 {rounded:,}억원"
    return f"{_fixed(value, Decimal('0.01'))}억원"


def _market_context(text: str, offset: int) -> bool:
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset][-80:]
    return bool(re.search(r"시장\s*(?:규모|전체|매출)?", prefix))


def _hira_cost_context(text: str, offset: int) -> bool:
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset]
    clause = re.split(r"(?<=[.!?])\s+|\|", prefix)[-1][-100:]
    return bool(re.search(r"보험자부담금|요양급여비용|진료비", clause))


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _fixed(value: Decimal, quantum: Decimal) -> str:
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{rounded:,.2f}"
