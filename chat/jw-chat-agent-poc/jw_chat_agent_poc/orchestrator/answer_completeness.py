from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Final, Sequence

from jw_chat_agent_poc.orchestrator.markdown_formatting import eok_value, pct_value, rank_value


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
class TopShareRow:
    rank: int
    brand: str
    share_text: str
    sales_text: str
    raw_share: Decimal | None = None


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


def deterministic_top_n_share_answer(
    question: str,
    fact_md: str,
    tool_calls: Sequence[dict[str, Any]],
) -> str:
    """Render top-N shares only when tool rows and verified facts agree."""

    if completeness_intent(question, fact_md) != "top_n_share_sum":
        return ""
    count = _requested_top_n(question)
    if not 1 <= count <= 20:
        return ""
    fact_rows = _top_share_fact_rows(fact_md, count)
    tool_rows = _top_share_tool_rows(tool_calls, count)
    if len(fact_rows) != count or not _same_top_share_surface(tool_rows, fact_rows):
        return ""
    try:
        total = sum(
            (row.raw_share for row in tool_rows if row.raw_share is not None),
            Decimal("0"),
        )
    except InvalidOperation:
        return ""
    if any(row.raw_share is None for row in tool_rows):
        return ""
    lines = [
        f"상위 {count}개 합계 시장점유율은 {total:.2f}%입니다.",
        "",
        "| 순위 | 브랜드 | 점유율 | 매출 |",
        "| --- | --- | --- | --- |",
        *(f"| {row.rank}위 | {row.brand} | {row.share_text} | {row.sales_text} |" for row in fact_rows),
    ]
    answer = "\n".join(lines)
    contract_block = _top_sum_block(_share_trends(fact_md), count)
    if contract_block:
        contract_lines = contract_block.splitlines()
        contract_lines[0] = lines[0]
        contract_block = "\n".join(contract_lines)
    return answer if contract_block and _surface_complete("top_n_share_sum", answer, contract_block) else ""


def deterministic_single_period_sales_answer(
    question: str,
    fact_md: str,
    tool_calls: Sequence[dict[str, Any]],
) -> str:
    """Render one verified sales value without a final LLM call."""

    if not any(token in question for token in ("매출", "판매")):
        return ""
    if any(
        token in question
        for token in (
            "비교",
            "각각",
            "추이",
            "변화",
            "증감",
            "최근",
            "대비",
            "차이",
            "점유율",
            "시장규모",
            "순위",
        )
    ):
        return ""
    requested_period = _explicit_period(question)
    if not requested_period:
        return ""
    matches: list[tuple[str, str]] = []
    failed_statuses = {"error", "query_failed", "unsupported", "mapping_failed", "missing"}
    for call in tool_calls:
        if str(call.get("tool") or "") != "get_brand_metric":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict) or str(data.get("metric") or "") != "sales":
            continue
        if str(data.get("status") or "ok").lower() in failed_statuses:
            continue
        brand = str(data.get("brand") or data.get("requested_brand") or "").strip()
        period = str(data.get("period") or "").strip()
        if not brand or brand not in question or period != requested_period:
            continue
        sales_text = eok_value(data.get("sales_억원"), data.get("sales_krw"))
        if not sales_text or not _verified_metric_fact(fact_md, brand, period, sales_text):
            continue
        matches.append((brand, sales_text))
    if len(matches) != 1:
        return ""
    brand, sales_text = matches[0]
    return f"{requested_period} {brand} 매출은 {sales_text}입니다."


def _explicit_period(question: str) -> str:
    compact = re.sub(r"\s+", "", question)
    quarter = re.search(r"(20\d{2})(?:년)?([1-4])분기", compact)
    if quarter:
        return f"{quarter.group(1)}-Q{quarter.group(2)}"
    canonical_quarter = re.search(r"(20\d{2})-?Q([1-4])", compact, flags=re.IGNORECASE)
    if canonical_quarter:
        return f"{canonical_quarter.group(1)}-Q{canonical_quarter.group(2)}"
    month = re.search(r"(20\d{2})년(1[0-2]|[1-9])월", compact)
    if month:
        return f"{month.group(1)}-{int(month.group(2)):02d}"
    canonical_month = re.search(r"(20\d{2})-(1[0-2]|0[1-9])", compact)
    if canonical_month:
        return canonical_month.group(0)
    year = re.search(r"(20\d{2})년", compact)
    return year.group(1) if year else ""


def _verified_metric_fact(fact_md: str, brand: str, period: str, sales_text: str) -> bool:
    headings = re.finditer(
        rf"^###\s+{re.escape(brand)}\s+지표 fact\s*$",
        fact_md,
        flags=re.MULTILINE,
    )
    for heading in headings:
        section = fact_md[heading.end() :]
        section = section.split("\n### ", 1)[0]
        rows: dict[str, str] = {}
        for line in section.splitlines():
            cells = _cells(line)
            if len(cells) >= 2:
                rows[cells[0]] = cells[1]
        if (
            rows.get("브랜드/시장") == brand
            and rows.get("지표") == "sales"
            and rows.get("기간") == period
            and rows.get("매출") == sales_text
        ):
            return True
    return False


def comparison_subjects(question: str) -> tuple[str, ...]:
    """Return the explicit operands of a brand-comparison question."""

    return _comparison_subjects(question)


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


def _top_share_fact_rows(fact_md: str, count: int) -> tuple[TopShareRow, ...]:
    rows: list[TopShareRow] = []
    active = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            active = "상위" in stripped and "점유율 추이 fact" in stripped
            continue
        if not active:
            continue
        cells = _cells(stripped)
        if len(cells) < 6 or not cells[0].isdigit():
            continue
        share_text = _last_percent_text(cells[3])
        sales_text = _eok_text(cells[5])
        if not share_text or not sales_text:
            return ()
        rows.append(TopShareRow(int(cells[0]), cells[1], share_text, sales_text))
        if len(rows) == count:
            break
    expected_ranks = tuple(range(1, count + 1))
    if tuple(row.rank for row in rows) != expected_ranks or len({row.brand for row in rows}) != count:
        return ()
    return tuple(rows)


def _top_share_tool_rows(tool_calls: Sequence[dict[str, Any]], count: int) -> tuple[TopShareRow, ...]:
    failed_statuses = {"error", "query_failed", "unsupported", "mapping_failed", "missing"}
    for call in tool_calls:
        if str(call.get("tool") or "") != "get_brand_metric":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict) or str(data.get("metric") or "") != "market_top_brands":
            continue
        if str(data.get("status") or "ok").lower() in failed_statuses:
            continue
        segments = data.get("level_segments")
        if not isinstance(segments, list) or len(segments) < count:
            continue
        rows: list[TopShareRow] = []
        for item in segments[:count]:
            if not isinstance(item, dict):
                return ()
            rank_text = rank_value(item.get("rank"), None)
            brand = str(item.get("name") or item.get("brand") or "").strip()
            share_text = pct_value(item.get("ms_recent_pct"))
            sales_text = eok_value(item.get("value_억원"), item.get("value"))
            if not rank_text.isdigit() or not brand or not share_text or not sales_text:
                return ()
            try:
                raw_share = Decimal(str(item.get("ms_recent_pct")))
            except InvalidOperation:
                return ()
            rows.append(TopShareRow(int(rank_text), brand, share_text, sales_text, raw_share))
        return tuple(rows)
    return ()


def _same_top_share_surface(
    tool_rows: Sequence[TopShareRow],
    fact_rows: Sequence[TopShareRow],
) -> bool:
    return tuple(
        (row.rank, row.brand, row.share_text, row.sales_text) for row in tool_rows
    ) == tuple(
        (row.rank, row.brand, row.share_text, row.sales_text) for row in fact_rows
    )


def _last_percent_text(value: str) -> str:
    matches = re.findall(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*%(?!p)", value)
    return f"{matches[-1]}%" if matches else ""


def _eok_text(value: str) -> str:
    match = re.fullmatch(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*억원", value.strip())
    return f"{match.group(1)}억원" if match else ""


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
        return _top_sum_surface_complete(answer, block)
    required_lines = tuple(line for line in block.splitlines() if line.startswith("| ") and "---" not in line)
    if required_lines:
        return all(line in answer for line in required_lines)
    return all(token in answer for token in _required_tokens(block))


def _top_sum_surface_complete(answer: str, block: str) -> bool:
    block_lines = block.splitlines()
    if not block_lines or not re.search(
        r"상위\s+\d+개\s+합계\s+시장점유율은\s+\d+(?:\.\d+)?%입니다\.",
        answer,
    ):
        return False
    answer_rows = tuple(_cells(line) for line in answer.splitlines())
    if not any(
        len(cells) >= 3
        and cells[0] == "순위"
        and cells[1] == "브랜드"
        and any("점유율" in cell for cell in cells[2:])
        for cells in answer_rows
    ):
        return False
    expected_rows = tuple(
        cells
        for line in block_lines
        if (cells := _cells(line)) and cells[0].isdigit()
    )
    for rank, brand, expected_share, *_rest in expected_rows:
        share_text = _last_percent_text(expected_share)
        if not any(
            len(cells) >= 3
            and cells[0].removesuffix("위") == rank
            and cells[1] == brand
            and any(_last_percent_text(cell) == share_text for cell in cells[2:])
            for cells in answer_rows
        ):
            return False
    return bool(expected_rows)


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
