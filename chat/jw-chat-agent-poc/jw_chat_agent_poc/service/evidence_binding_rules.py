from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact, number_tokens
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.tool_use.routing_v4_rules import explicit_disease_code


IDENTIFIER_KEYS: Final[tuple[str, ...]] = (
    "sick_cd",
    "nct_id",
    "brand_key",
    "brand",
    "market_id",
    "atc4_code",
)

_METRIC_TERMS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("시장 구성 브랜드 수", ("시장 구성 브랜드 수", "브랜드 수", "분모")),
    ("초과성장", ("초과성장", "excess growth")),
    ("매출 변화율", ("매출 변화율", "성장률")),
    ("점유율 변화", ("점유율 변화", "점유율 증감")),
    ("환자수", ("환자수", "환자 수")),
    ("시장점유율", ("시장점유율", "점유율", "ms")),
    ("시장규모", ("시장규모", "시장 규모")),
    ("매출", ("매출", "sales")),
    ("HHI", ("hhi", "집중도")),
    ("CR5", ("cr5", "집중도")),
    ("CAGR", ("cagr", "연평균")),
    ("순위", ("순위", "rank")),
    ("기간", ("기준기간", "기간")),
)
# Units whose base metric is unambiguous. Percent shapes are absent on purpose:
# "%" is shared by 시장점유율 / CAGR / 매출 변화율, and "%p" belongs to a derived
# metric, so narrowing either would release a deliberate block. The first entry
# of each tuple is the canonical choice when a sentence names no compatible one.
_BASE_METRICS_BY_UNIT: Final[dict[str, tuple[str, ...]]] = {
    "억원": ("매출", "시장규모"),
    "원": ("매출", "시장규모"),
}
#: An ordinal that numbers an item rather than measuring anything: "1." or "2)"
#: followed by space and then the item's text. Anchored at the start of a line,
#: of a table cell, or of a bulleted item — the same marker in all three places.
#: It used to be anchored to the start of a LINE only, which meant a numbered
#: cell inside a table ("| 1. 미보유 데이터 |") kept its ordinal and every one of
#: those ordinals became a claim no fact could attest. A digit followed
#: immediately by a decimal point and more digits is a value, not a marker, so
#: the required whitespace after the separator keeps "1.5" out.
_ORDERED_LIST_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)(?:^|(?<=\|))[ \t]*(?:[-*•][ \t]*)?\d+[.)]\s+"
)
#: A band or code range used as a row label: "0-9세", "30-39", "E10-E14". The
#: boundaries name the bucket; the count beside them is the measurement. Written
#: as a shape rather than a list of buckets so a range nobody has seen still
#: matches, and applied only after periods have been claimed so that a period
#: written with a dash is never mistaken for one of these.
_RANGE_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.])\d{1,3}\s*[-~]\s*\d{1,3}\s*(?:세|대|개월|년)?(?![\d.])"
    r"|(?<![A-Za-z0-9])[A-Z]\d{2}\s*[-~]\s*[A-Z]\d{2}(?![A-Za-z0-9])"
)
_NUMBER_OCCURRENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<value>[+-]?\d[\d,]*(?:\.\d+)?)"
    r"\s*"
    r"(?P<unit>개월|시간|μg|ug|mg|mL|ml|μL|uL|%p|건|개|편|명|차례|주|일|회|g|%)?"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_POSOLOGY_UNITS: Final[frozenset[str]] = frozenset(
    {"개월", "시간", "μg", "ug", "mg", "ml", "μl", "ul", "주", "일", "회", "g"}
)
_POSOLOGY_CONTEXT_TERMS: Final[tuple[str, ...]] = (
    "용법",
    "용량",
    "투여",
    "주사",
    "복용",
    "간격",
    "매월",
    "매일",
    "함량",
    "농도",
)
_FACTUAL_PERCENT_CONTEXT_TERMS: Final[tuple[str, ...]] = (
    "점유율",
    "비율",
    "성장률",
    "증가율",
    "감소율",
)
_SAME_ENTITY_OPERAND_METRICS: Final[frozenset[str]] = frozenset(
    {"점유율 변화", "매출 변화", "매출 변화율", "CAGR"}
)
_EXPECTED_OPERAND_METRICS: Final[dict[str, frozenset[str]]] = {
    "점유율 변화": frozenset({"시장점유율"}),
    "매출 변화": frozenset({"매출"}),
    "매출 변화율": frozenset({"매출"}),
    "CAGR": frozenset({"매출"}),
    "초과성장": frozenset({"CAGR", "매출 변화율"}),
}


def normalized_entity(value: str) -> str:
    stripped = str(value).strip().upper()
    code = explicit_disease_code(stripped)
    return code or stripped.casefold()


def expected_entity_set(question: str, values: Sequence[str]) -> set[str]:
    expected = {normalized_entity(value) for value in values if str(value).strip()}
    explicit = explicit_disease_code(question)
    if explicit:
        expected.add(normalized_entity(explicit))
    return expected


def question_metrics(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    metrics = [
        metric
        for metric, terms in _METRIC_TERMS
        if any(term.casefold() in lowered for term in terms)
    ]
    return tuple(dict.fromkeys(metrics))


def claim_metrics_for_token(answer: str, token: str) -> tuple[str, ...]:
    segments = re.split(r"(?<=[.!?。])\s+|\n+", answer)
    for segment in segments:
        if token in claim_number_tokens(segment):
            metrics = _metrics_consistent_with_token(question_metrics(segment), token)
            if metrics:
                return metrics
    return _table_header_metrics_for_token(answer, token)


def _metrics_consistent_with_token(
    metrics: tuple[str, ...],
    token: str,
) -> tuple[str, ...]:
    """Keep only the metrics the token's own shape can actually carry.

    A sentence is scanned as a whole, so every metric word in it is returned for
    every number in it. That is how a currency amount standing in a sentence
    about 점유율 ends up expected as 시장점유율 while its unit says 억원 — the two
    axes are read at different scopes and nothing reconciles them.

    Only shapes with an unambiguous base metric take part: a period token, and
    the currency units. Percent shapes are deliberately left alone; they are
    shared by 시장점유율, CAGR and 매출 변화율, and narrowing them would also turn a
    derived-metric expectation into one that matches its own derived fact, which
    would release the deliberate F66 blocks.

    The mapping stays on BASE metrics for the same reason: expecting 매출 keeps a
    매출 변화 fact refused, and expecting 시장점유율 keeps a 점유율 변화 fact refused.
    """
    if not metrics:
        return metrics
    if explicit_periods(token):
        return ("기간",)
    allowed = _BASE_METRICS_BY_UNIT.get(token_unit(token))
    if not allowed:
        return metrics
    narrowed = tuple(metric for metric in metrics if metric in allowed)
    if narrowed:
        return narrowed
    # The sentence named a metric the token cannot be. Trust the token.
    return (allowed[0],)


def _table_header_metrics_for_token(answer: str, token: str) -> tuple[str, ...]:
    # Accumulate header metrics across *every* table that carries the token, not
    # only the first one. When two market scopes are merged into a single answer
    # under the same public 뷰 label, the same numeric token can appear under a
    # foreign scope's column header (e.g. 시장규모) as well as its own (매출).
    # Returning only the first table's header let the foreign scope hijack the
    # claim's metric and drop the correct-scope fact (F24 RC1). The union keeps
    # the correct metric available; entity/period/unit/scope binding still decides.
    lines = answer.splitlines()
    metrics: list[str] = []
    for row_index, line in enumerate(lines):
        cells = _markdown_table_cells(line)
        if not cells or not any(token in claim_number_tokens(cell) for cell in cells):
            continue

        table_start = row_index
        while table_start > 0 and _markdown_table_cells(lines[table_start - 1]):
            table_start -= 1
        if table_start == row_index:
            continue

        headers = _markdown_table_cells(lines[table_start])
        for column, cell in enumerate(cells):
            if column >= len(headers) or token not in claim_number_tokens(cell):
                continue
            metrics.extend(question_metrics(headers[column]))
    return tuple(dict.fromkeys(metrics))


def _markdown_table_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip() for cell in stripped[1:-1].split("|"))


def claim_number_tokens(text: str) -> tuple[str, ...]:
    without_markers = _ORDERED_LIST_MARKER_RE.sub("", text)
    periods = explicit_periods(without_markers)
    without_periods = without_markers
    for period in sorted(periods, key=len, reverse=True):
        without_periods = re.sub(
            re.escape(period),
            " ",
            without_periods,
            flags=re.IGNORECASE,
        )
    # Range labels name the row, they do not measure it. Removed only after the
    # period pass above, so a period that looks like a range ("2020-2024",
    # "2026-01 ~ 2026-05") has already been claimed as a period and is not
    # touched here. The measurement beside the label is untouched: "0-9세" stops
    # contributing 0 and -9 while "1,379명" in the same row stays a claim.
    without_labels = _RANGE_LABEL_RE.sub(" ", without_periods)
    return tuple(dict.fromkeys((*number_tokens(without_labels), *periods)))


def excluded_label_token_count(text: str) -> int:
    """How many numeric tokens this text lost to ordinal and range-label stripping.

    A count, never the tokens: the values would carry answer text across the
    trace boundary. Reported so that "nothing was blocked" and "everything that
    could have been blocked was excluded" stay distinguishable — an answer whose
    every number turned out to be a row label should say so rather than look
    clean.
    """
    return max(0, len(number_tokens(text)) - len(claim_number_tokens(text)))


def binding_claim_number_tokens(text: str) -> tuple[str, ...]:
    tokens = claim_number_tokens(text)
    occurrence_types: dict[str, set[str]] = {}
    for match in _NUMBER_OCCURRENCE_RE.finditer(text):
        normalized = number_tokens(match.group(0))
        if not normalized:
            continue
        occurrence_type = (
            "narrative"
            if _is_posology_occurrence(text, match)
            else "factual"
        )
        for token in normalized:
            if token in tokens:
                occurrence_types.setdefault(token, set()).add(occurrence_type)
    return tuple(
        token
        for token in tokens
        if occurrence_types.get(token) != {"narrative"}
    )


def _is_posology_occurrence(text: str, match: re.Match[str]) -> bool:
    unit = str(match.group("unit") or "").casefold()
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end].casefold()

    if "적응증" in line and not unit:
        return True
    if unit == "%":
        if any(term in line for term in _FACTUAL_PERCENT_CONTEXT_TERMS):
            return False
        return any(term in line for term in ("함량", "농도"))
    if unit not in _POSOLOGY_UNITS:
        return False
    if unit in {"μg", "ug", "mg", "ml", "μl", "ul", "g"}:
        return True
    return any(term in line for term in _POSOLOGY_CONTEXT_TERMS)


def explicit_periods(text: str) -> tuple[str, ...]:
    periods: list[str] = []
    quarter_matches = tuple(
        re.finditer(
            r"(?<!\d)(20\d{2})\s*(?:년\s*)?(?:Q\s*([1-4])|([1-4])\s*분기)",
            text,
            re.IGNORECASE,
        )
    )
    for match in quarter_matches:
        year = match.group(1)
        quarter = int(match.group(2) or match.group(3))
        first_month = (quarter - 1) * 3 + 1
        periods.append(f"{year}-Q{quarter}")
        periods.extend(
            f"{year}-{month:02d}"
            for month in range(first_month, first_month + 3)
        )
    periods.extend(
        re.findall(
            r"(?<!\d)20\d{2}-(?:0[1-9]|1[0-2]|Q[1-4])(?!\d)",
            text,
            re.IGNORECASE,
        )
    )
    periods.extend(
        re.findall(
            r"(?<!\d)20\d{2}(?!\d|-(?:0[1-9]|1[0-2]|Q[1-4]))",
            text,
            re.IGNORECASE,
        )
    )
    return tuple(dict.fromkeys(period.upper() for period in periods))


def entity_matches(fact: EvidenceFact, expected: set[str]) -> bool:
    if not expected:
        return True
    if not fact.entity:
        return False
    return normalized_entity(fact.entity) in expected


def metric_matches(fact: EvidenceFact, expected: tuple[str, ...]) -> bool:
    if not expected:
        return True
    if expected == ("기간",):
        return True
    return fact.metric in expected or fact.metric in {"기간", "질병코드"}


def period_matches(fact: EvidenceFact, requested: tuple[str, ...]) -> bool:
    if not requested or fact.metric in {"기간", "질병코드"}:
        return True
    if not present(fact.period):
        return False
    fact_periods = set(explicit_periods(fact.period))
    return bool(fact_periods.intersection(requested))


def unit_matches(fact: EvidenceFact, token: str) -> bool:
    expected = token_unit(token)
    if not expected or fact.metric in {"기간", "질병코드"}:
        return True
    if not present(fact.unit):
        return False
    if expected == "%":
        return fact.unit in {"%", "%p"}
    return fact.unit == expected


_VIEW_SCOPE_TERMS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("general_view", ("일반뷰", "일반view", "atc4", "atc기준")),
    ("competitive_dynamics", ("경쟁군", "경쟁시장", "competitive_dynamics", "competitive", "cd기준")),
    ("market_landscape", ("전략뷰", "전략view", "market_landscape", "ml기준")),
)


def fact_scope(fact: EvidenceFact) -> str:
    """Internal scope signature of a fact.

    The public 뷰 label is shared across distinct scopes, so the analytic
    ``view`` (view_type, optionally suffixed with an internal market key) is the
    scope carrier. ``market_id`` itself is never surfaced; it stays inside this
    opaque signature and is only ever compared, never rendered.
    """
    view = fact.view.strip() if present(fact.view) else ""
    market_id = _fact_market_id(fact)
    if not market_id:
        return view
    view_base = _scope_base(view)
    return f"{view_base}:{market_id}" if view_base else market_id


def _scope_base(scope: str) -> str:
    return scope.split(":", 1)[0].strip()


def _fact_market_id(fact: EvidenceFact) -> str:
    if present(fact.market_id):
        return fact.market_id.strip().casefold()
    scope_parts = fact.view.split(":", 1)
    return scope_parts[1].strip().casefold() if len(scope_parts) == 2 else ""


def question_view_scopes(question: str) -> frozenset[str]:
    """View scopes explicitly requested by the question (empty when unspecified)."""
    lowered = question.casefold()
    return frozenset(
        view
        for view, terms in _VIEW_SCOPE_TERMS
        if any(term.casefold() in lowered for term in terms)
    )


def scope_matches(
    fact: EvidenceFact,
    expected_scopes: frozenset[str],
    expected_market_ids: frozenset[str] = frozenset(),
) -> bool:
    """Scope binding: view and internal market identity must match the request.

    View matching keeps F24's permissive legacy behavior, and already skips
    facts that carry no scope at all. When resolution pins a market identity a
    foreign one still fails closed, and so does a missing one -- but only for
    facts whose builder could have supplied it. A fact projected from a tool
    envelope has no market_id in its schema at all, so demanding one asks the
    source for something it cannot express. Such a fact is exempt from this
    axis, the way 기간 and 질병코드 facts are exempt from the metric, period
    and unit axes above.
    """
    scope = fact_scope(fact)
    if expected_scopes and scope:
        if scope not in expected_scopes and _scope_base(scope) not in expected_scopes:
            return False
    if expected_market_ids:
        market_id = _fact_market_id(fact)
        if not market_id:
            return not fact.market_scope_capable
        return market_id in expected_market_ids
    return True


def scope_axis_exempt(
    fact: EvidenceFact,
    expected_market_ids: frozenset[str] = frozenset(),
) -> bool:
    """Whether the market axis was waived for a fact rather than satisfied.

    Exposed so a waiver is visible in binding diagnostics instead of being
    indistinguishable from a match.
    """
    if not expected_market_ids:
        return False
    return not _fact_market_id(fact) and not fact.market_scope_capable


def mismatch_reason(
    candidates: Sequence[EvidenceFact],
    expected_entities: set[str],
    expected_metrics: tuple[str, ...],
    *,
    requested_periods: tuple[str, ...],
    token: str,
    expected_scopes: frozenset[str] = frozenset(),
    expected_market_ids: frozenset[str] = frozenset(),
) -> str:
    if expected_entities and not any(
        entity_matches(fact, expected_entities) for fact in candidates
    ):
        return "ENTITY_MISMATCH"
    if expected_metrics and not any(
        metric_matches(fact, expected_metrics) for fact in candidates
    ):
        return "METRIC_MISMATCH"
    if requested_periods and not any(
        period_matches(fact, requested_periods) for fact in candidates
    ):
        return "PERIOD_MISMATCH"
    if not any(unit_matches(fact, token) for fact in candidates):
        return "UNIT_MISMATCH"
    if (expected_scopes or expected_market_ids) and not any(
        scope_matches(fact, expected_scopes, expected_market_ids)
        for fact in candidates
    ):
        return "SCOPE_MISMATCH"
    return "BINDING_MISMATCH"


def grade(fact: EvidenceFact) -> SourceGrade:
    try:
        return SourceGrade(fact.source_grade)
    except ValueError:
        return SourceGrade.UNVERIFIED


def operand_binding_outcome(
    fact: EvidenceFact,
    facts_by_id: Mapping[str, EvidenceFact],
) -> tuple[str, str]:
    if not fact.operand_fact_ids:
        return "pass", ""
    operands = tuple(facts_by_id.get(fact_id) for fact_id in fact.operand_fact_ids)
    if any(operand is None for operand in operands):
        return "fail", "MISSING_OPERAND"
    bound_operands = tuple(operand for operand in operands if operand is not None)

    expected_metrics = _EXPECTED_OPERAND_METRICS.get(fact.metric)
    if expected_metrics:
        if any(not present(operand.metric) for operand in bound_operands):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        if any(operand.metric not in expected_metrics for operand in bound_operands):
            return "fail", "OPERAND_METRIC_MISMATCH"

    if fact.metric in _SAME_ENTITY_OPERAND_METRICS and present(fact.entity):
        if any(not present(operand.entity) for operand in bound_operands):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        expected_entity = normalized_entity(fact.entity)
        if any(normalized_entity(operand.entity) != expected_entity for operand in bound_operands):
            return "fail", "OPERAND_ENTITY_MISMATCH"

    derived_periods = set(explicit_periods(fact.period))
    if derived_periods:
        operand_period_sets = tuple(set(explicit_periods(operand.period)) for operand in bound_operands)
        if any(not periods for periods in operand_period_sets):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        if any(not periods.issubset(derived_periods) for periods in operand_period_sets):
            return "fail", "OPERAND_PERIOD_MISMATCH"

    if present(fact.view):
        if any(not present(operand.view) for operand in bound_operands):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        if any(operand.view != fact.view for operand in bound_operands):
            return "fail", "OPERAND_VIEW_MISMATCH"

    if any(not present(operand.source_grade) for operand in bound_operands):
        return "partial", "INCOMPLETE_OPERAND_BINDING"
    if any(grade(operand) is not grade(fact) for operand in bound_operands):
        return "fail", "OPERAND_SOURCE_GRADE_MISMATCH"
    return "pass", ""


def has_binding_metadata(fact: EvidenceFact) -> bool:
    return present(fact.entity) or present(fact.metric) or present(fact.source_grade)


def present(value: str) -> bool:
    return bool(value and value != "-")


def requested_period_unavailable(answer: str, requested: tuple[str, ...]) -> bool:
    if not any(period in answer.upper() for period in requested):
        return False
    return any(
        marker in answer
        for marker in (
            "조회 실패",
            "표시하지 않습니다",
            "확인할 수 없습니다",
            "데이터가 없습니다",
            "미보유",
        )
    )


def without_bound_identifiers(answer: str, expected: set[str]) -> str:
    stripped = answer
    for entity in expected:
        compact = entity.replace(".", "")
        for variant in (entity, compact):
            stripped = re.sub(re.escape(variant), "ENTITY", stripped, flags=re.IGNORECASE)
    return stripped


def token_unit(token: str) -> str:
    normalized = token.replace(" ", "")
    for suffix in ("억원", "억 원", "%p", "%", "명", "건", "개", "위", "원"):
        if normalized.endswith(suffix.replace(" ", "")):
            return suffix.replace(" ", "")
    return ""
