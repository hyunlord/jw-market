from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final


COMPLETENESS_INTENTS: Final[tuple[str, ...]] = (
    "brand_compare",
    "share_delta_compare",
    "top_n_share_sum",
    "concentration",
    "target_share_gap",
    "channel_provenance",
)


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    period: str
    sales_text: str
    sales: float
    share_text: str


@dataclass(frozen=True, slots=True)
class BrandSeries:
    brand: str
    points: tuple[SeriesPoint, ...]


@dataclass(frozen=True, slots=True)
class ShareTrend:
    brand: str
    start: str
    latest: str
    delta: str


@dataclass(frozen=True, slots=True)
class TargetInputs:
    period: str
    sales_text: str
    sales: float
    share_text: str
    market_text: str
    market: float


def completeness_intent(question: str, fact_md: str = "") -> str | None:
    """Classify only the six deterministic completeness question families."""

    text = question.lower()
    if re.search(r"\d+(?:\.\d+)?\s*%", text) and "점유율" in text and any(
        token in text for token in ("달성", "회복", "필요", "늘어", "되려면", "목표")
    ):
        return "target_share_gap"
    if any(token in text for token in ("집중도", "집중", "분산", "편중")):
        return "concentration"
    if _requested_top_n(text) and "점유율" in text and any(token in text for token in ("합산", "합계", "비중", "차지")):
        return "top_n_share_sum"
    if "점유율" in text and any(token in text for token in ("변화", "증감", "추이")) and any(
        token in text for token in ("상위", "비교", "각각")
    ):
        return "share_delta_compare"
    if "매출" in text and any(token in text for token in ("비교", "각각")):
        return "brand_compare"
    if _is_explicit_brand_compare(question, fact_md):
        return "brand_compare"
    if "채널" in text and any(token in text for token in _CHANNELS):
        return "channel_provenance"
    return None


def repair_completeness(intent: str, question: str, answer: str, fact_md: str) -> str:
    """Append a deterministic completion only when all required facts parse."""

    block = _completion_block(intent, question, fact_md)
    if intent == "brand_compare" and not block:
        return _brand_compare_unavailable()
    if not block or _surface_complete(intent, answer, block):
        return answer
    if intent == "top_n_share_sum":
        return _join(block, answer)
    return _insert_before_source(answer, block)


def completeness_status(intent: str, question: str, answer: str, fact_md: str) -> str:
    """Return trace status without changing the response."""

    block = _completion_block(intent, question, fact_md)
    if not block:
        return "missing_required_fact"
    return "pass" if _surface_complete(intent, answer, block) else "surface_missing"


def _completion_block(intent: str, question: str, fact_md: str) -> str:
    if intent == "brand_compare":
        series = _brand_series(fact_md)
        requested = tuple(item for item in series if item.brand in question)
        return _brand_compare_block(requested if requested else series)
    if intent == "share_delta_compare":
        return _share_delta_block(_share_trends(fact_md), _requested_top_n(question))
    if intent == "top_n_share_sum":
        return _top_sum_block(_share_trends(fact_md), _requested_top_n(question))
    if intent == "concentration":
        return _concentration_block(_share_trends(fact_md), _hhi(fact_md))
    if intent == "target_share_gap":
        return _target_gap_block(_target_inputs(fact_md), _target_share(question))
    if intent == "channel_provenance":
        return _channel_block(question, fact_md)
    return ""


def _brand_series(fact_md: str) -> tuple[BrandSeries, ...]:
    lines = fact_md.splitlines()
    result: list[BrandSeries] = []
    for index, line in enumerate(lines):
        match = re.match(r"^###\s+(.+?)\s+매출 시계열 fact\s*$", line.strip())
        if match is None:
            continue
        points: list[SeriesPoint] = []
        for raw in lines[index + 1 :]:
            if raw.strip().startswith("### "):
                break
            cells = _cells(raw)
            if len(cells) < 3 or not re.match(r"^20\d{2}", cells[0]):
                continue
            points.append(SeriesPoint(cells[0], cells[1], _number(cells[1]), cells[2]))
        if len(points) >= 2:
            result.append(BrandSeries(match.group(1).strip(), tuple(points)))
    return tuple(result)


def _share_trends(fact_md: str) -> tuple[ShareTrend, ...]:
    result: list[ShareTrend] = []
    active = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            active = "점유율 추이 fact" in stripped
            continue
        if not active:
            continue
        cells = _cells(stripped)
        if len(cells) >= 5 and cells[0].isdigit():
            result.append(ShareTrend(cells[1], cells[2], cells[3], cells[4]))
    return tuple(result)


def _target_inputs(fact_md: str) -> TargetInputs | None:
    result: TargetInputs | None = None
    for line in fact_md.splitlines():
        cells = _cells(line)
        if len(cells) < 4 or not re.match(r"^20\d{2}", cells[0]):
            continue
        if "억원" not in cells[1] or "%" not in cells[2] or "억원" not in cells[3]:
            continue
        result = TargetInputs(cells[0], cells[1], _number(cells[1]), cells[2], cells[3], _number(cells[3]))
    if result is not None:
        return result
    series = _brand_series(fact_md)
    if not series:
        return None
    latest = series[0].points[-1]
    market_text = _latest_market_size(fact_md, latest.period)
    if not market_text:
        return None
    return TargetInputs(latest.period, latest.sales_text, latest.sales, latest.share_text, market_text, _number(market_text))


def _latest_market_size(fact_md: str, period: str) -> str:
    active = False
    latest = ""
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            active = "시장규모 시계열 fact" in stripped
            continue
        if not active:
            continue
        cells = _cells(stripped)
        if len(cells) >= 2 and cells[0] == period and "억원" in cells[1]:
            latest = cells[1]
    return latest


def _brand_compare_block(series: tuple[BrandSeries, ...]) -> str:
    if len(series) < 2:
        return ""
    lines = ["## 브랜드 매출 비교", "| 브랜드 | 시작 기간 | 시작 매출 | 최신 기간 | 최신 매출 | 증감액 | 증감률 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    directions: list[str] = []
    for item in series:
        first, last = item.points[0], item.points[-1]
        delta = last.sales - first.sales
        rate = delta / first.sales * 100 if first.sales else 0.0
        lines.append(f"| {item.brand} | {first.period} | {first.sales_text} | {last.period} | {last.sales_text} | {_signed(delta)}억원 | {_signed(rate)}% |")
        directions.append(f"{item.brand}는 {'상승' if delta >= 0 else '하락'}")
    lines.extend(("", "## 브랜드 점유율 비교", "| 브랜드 | 시작 점유율 | 최신 점유율 |", "| --- | --- | --- |"))
    for item in series:
        first, last = item.points[0], item.points[-1]
        lines.append(f"| {item.brand} | {first.share_text} | {last.share_text} |")
    lines.append("\n" + ", ".join(directions) + "했습니다.")
    return "\n".join(lines)


def _brand_compare_unavailable() -> str:
    return (
        "요청한 브랜드 중 일부의 지표 조회가 완료되지 않아 비교를 완결하지 못했습니다. "
        "확인되지 않은 브랜드의 수치는 추정하지 않습니다."
    )


def _is_explicit_brand_compare(question: str, fact_md: str) -> bool:
    text = question.lower()
    if not any(token in text for token in ("비교", "각각", " vs ", "대비")):
        return False
    named_facts = tuple(item.brand for item in _brand_series(fact_md) if item.brand in question)
    if len(named_facts) >= 2:
        return True
    if "대비" in text and not any(token in text for token in ("비교", "각각", " vs ")):
        return False
    return len(_comparison_subjects(question)) >= 2


def _comparison_subjects(question: str) -> tuple[str, ...]:
    text = re.sub(r"[?!.]", " ", question).strip()
    patterns = (
        r"^(.+?)(?:와|과|랑|하고)\s*(.+?)(?:비교|각각|대비)",
        r"^(.+?)\s+vs\.?\s+(.+)$",
        r"^(.+?),\s*(.+?)\s+(?:각각|비교)",
        r"^(.+?)\s+대비\s+(.+?)(?:\s+비교|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is None:
            continue
        subjects = tuple(_clean_comparison_subject(group) for group in match.groups())
        if all(subjects):
            return subjects
    return ()


def _clean_comparison_subject(value: str) -> str:
    text = value.strip()
    text = re.sub(r"(?:을|를|은|는|이|가)$", "", text)
    text = re.sub(r"\s+(?:\d+\s*(?:개월|달|년)\s*)?(?:매출|점유율).*$", "", text)
    return text.strip()


def _share_delta_block(rows: tuple[ShareTrend, ...], count: int) -> str:
    selected = rows[:count]
    if len(selected) < count:
        return ""
    lines = ["## 상위 브랜드 점유율 변화", "| 브랜드 | 시작 점유율 | 최신 점유율 | 변화폭 | 방향 |", "| --- | --- | --- | --- | --- |"]
    for row in selected:
        direction = "하락" if _number(row.delta) < 0 else "상승"
        lines.append(f"| {row.brand} | {row.start} | {row.latest} | {row.delta} | {direction} |")
    return "\n".join(lines)


def _top_sum_block(rows: tuple[ShareTrend, ...], count: int) -> str:
    selected = rows[:count]
    if len(selected) < count:
        return ""
    total = sum(_percent(row.latest) for row in selected)
    lines = [f"상위 {count}개 합계 시장점유율은 {total:.2f}%입니다.", "", "| 순위 | 브랜드 | 최신 점유율 |", "| --- | --- | --- |"]
    lines.extend(f"| {index} | {row.brand} | {row.latest} |" for index, row in enumerate(selected, 1))
    return "\n".join(lines)


def _concentration_block(rows: tuple[ShareTrend, ...], hhi: float | None) -> str:
    if hhi is not None:
        conclusion = "집중" if hhi >= 2500 else "분산"
        return f"## 시장 집중도\n이 시장은 HHI 기준으로 **{conclusion}**되어 있습니다. 근거는 HHI {hhi:.2f}입니다."
    if len(rows) < 5:
        return ""
    cr3 = sum(_percent(row.latest) for row in rows[:3])
    cr5 = sum(_percent(row.latest) for row in rows[:5])
    conclusion = "집중" if cr5 >= 50 else "분산"
    return f"## 시장 집중도\n이 시장은 상위 브랜드 점유율 기준으로 **{conclusion}**되어 있습니다. 근거는 CR3 {cr3:.2f}%, CR5 {cr5:.2f}%입니다."


def _hhi(fact_md: str) -> float | None:
    for line in fact_md.splitlines():
        cells = _cells(line)
        if len(cells) >= 2 and cells[0].strip().upper() == "HHI" and re.search(r"\d", cells[1]):
            return _number(cells[1])
    return None


def _target_gap_block(inputs: TargetInputs | None, target: float | None) -> str:
    if inputs is None or target is None or inputs.sales <= 0:
        return ""
    target_sales = inputs.market * target / 100
    gap = target_sales - inputs.sales
    growth = gap / inputs.sales * 100
    return (
        f"## 목표 점유율 역산\n{inputs.period} 시장 규모 {inputs.market_text}를 기준으로, 목표 매출 = 시장 규모 x 목표 점유율 공식에 따라 "
        f"{target:.2f}% 기준 목표 매출 {target_sales:.2f}억원입니다. 현재 매출 {inputs.sales_text}({inputs.share_text}) 대비 "
        f"증분액 {_signed(gap)}억원, 증분률 {_signed(growth)}%가 필요합니다. 최근 기간 시장 규모 불변 가정입니다."
    )


_CHANNELS: Final[tuple[str, ...]] = ("상급종합병원", "상급종병", "종병", "병원", "의원", "약국", "원내", "원외", "보건소", "기타")


def _channel_block(question: str, fact_md: str) -> str:
    for channel in _CHANNELS:
        if channel in question and channel in fact_md:
            return f"## 필터 provenance\n- 적용 채널: {channel}"
    return ""


def _surface_complete(intent: str, answer: str, block: str) -> bool:
    if intent == "top_n_share_sum":
        required = (block.splitlines()[0], *(line for line in block.splitlines() if line.startswith("| ") and "---" not in line))
        return all(item in answer for item in required)
    required_lines = tuple(line for line in block.splitlines() if line.startswith("| ") and "---" not in line)
    if required_lines:
        return all(line in answer for line in required_lines)
    return all(token in answer for token in _required_tokens(block))


def _required_tokens(block: str) -> tuple[str, ...]:
    if "시장 집중도" in block:
        return tuple(re.findall(r"(?:집중|분산)|CR[35]\s+\d+(?:\.\d+)?%", block))
    if "목표 점유율 역산" in block:
        values = re.findall(r"(?:시장 규모|목표 매출|증분액|증분률)\s+[+]?\d+(?:\.\d+)?(?:억원|%)", block)
        return (*values, "시장 규모 불변 가정")
    if "필터 provenance" in block:
        return (block.split(": ", 1)[-1],)
    return (block,)


def _requested_top_n(text: str) -> int:
    match = re.search(r"상위\s*(\d+)\s*개?", text)
    return int(match.group(1)) if match else 5


def _target_share(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(match.group(1)) if match else None


def _cells(line: str) -> list[str]:
    if not line.strip().startswith("|") or "---" in line:
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _number(text: str) -> float:
    match = re.search(r"[+-]?\d[\d,]*(?:\.\d+)?", text)
    return float(match.group(0).replace(",", "")) if match else 0.0


def _percent(text: str) -> float:
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", text)
    return float(match.group(1)) if match else 0.0


def _signed(value: float) -> str:
    return f"{value:+.2f}"


def _insert_before_source(answer: str, block: str) -> str:
    marker = "\n## 출처"
    if marker not in answer:
        return _join(answer, block)
    head, tail = answer.split(marker, 1)
    return _join(head, block, "## 출처" + tail)


def _join(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block.strip())
