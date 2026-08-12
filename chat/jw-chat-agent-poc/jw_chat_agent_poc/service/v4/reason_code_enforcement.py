from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError

from jw_chat_agent_poc.service.v4.comparison_facts import symmetric_observation
from jw_chat_agent_poc.service.v4.contracts import (
    AbsenceConfirmation,
    SourceResult,
)


_REASON_CODES = (
    "UNSUPPORTED_TRANSFER_ATTRIBUTION",
    "ABSENCE_OVERCLAIM",
    "INTERNAL_TOKEN_LEAK",
    "AS_OF_DATE",
)
_TRANSFER_RE = re.compile(r"(?:이동|흡수|전환|잠식|대체)")
_ENTITY = r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9+._-]*?"
_TRANSFER_VERB = (
    r"(?:이동|흡수|전환|잠식|대체)(?:한|하였|했|된|됐)?"
    r"(?:\s*것으로\s*(?:확인|보|판단|추정|해석))?"
    r"(?:되었습니다|됐습니다|됩니다|입니다|습니다|다)?(?![가-힣A-Za-z0-9])[.!?]?"
)
_DIRECTED_TRANSFER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?P<from>{_ENTITY})\s*감소분(?:이|은|는)?\s*"
        rf"(?P<to>{_ENTITY})(?:으로|로)\s*{_TRANSFER_VERB}"
    ),
    re.compile(
        rf"(?P<from>{_ENTITY})(?:에서|로부터)\s*"
        rf"(?P<to>{_ENTITY})(?:으로|로)\s*{_TRANSFER_VERB}"
    ),
    re.compile(
        rf"(?P<from>{_ENTITY})\s*(?:→|->)\s*"
        rf"(?P<to>{_ENTITY})\s*{_TRANSFER_VERB}"
    ),
    re.compile(
        rf"(?P<from>{_ENTITY})(?:을|를)\s*"
        rf"(?P<to>{_ENTITY})(?:으로|로)\s*{_TRANSFER_VERB}"
    ),
)
_TRANSFER_UNCERTAINTY_RE = re.compile(
    r"(?:원인|이동|전환|흡수|잠식|대체)[^.\n]{0,50}"
    r"(?:확인되지|미확인|확정할\s*수\s*없)|"
    r"(?:확인되지|미확인|확정할\s*수\s*없)[^.\n]{0,50}"
    r"(?:원인|이동|전환|흡수|잠식|대체)"
)
_TRANSFER_BOUNDED_RE = re.compile(
    r"(?:확인되지|미확인|추가\s*확인(?:이)?\s*필요|"
    r"단정[^.\n]{0,30}(?:근거[^.\n]{0,15}부족|어렵|할\s*수\s*없)|"
    r"근거[^.\n]{0,20}부족|판단[^.\n]{0,20}(?:어렵|할\s*수\s*없))"
)
_UNDIRECTED_TRANSFER_OVERCLAIM_RE = re.compile(
    r"(?:시장\s*(?:잠식|흡수)|"
    r"(?:매출|점유율|감소분|처방량)[^.\n]{0,40}(?:이동|흡수|전환|잠식|대체)|"
    r"(?:이동|흡수|전환|잠식|대체)[^.\n]{0,40}(?:매출|점유율|감소분|처방량))"
)
_ABSENCE_CERTAINTY_RE = re.compile(
    r"(?:비급여(?:입니다|로\s*확인|로\s*분류|에\s*해당)|"
    r"급여\s*기준이?\s*없습니다|허가\s*문서가?\s*없습니다)"
)
_ABSENCE_UNCERTAINTY_RE = re.compile(
    r"(?:비급여|급여|허가)[^.\n]{0,50}(?:확인되지|미확인|확정할\s*수\s*없)|"
    r"(?:확인되지|미확인|확정할\s*수\s*없)[^.\n]{0,50}(?:비급여|급여|허가)"
)
_FUTURE_DATE_RE = re.compile(r"(?:예정|다가오|앞두고)")
_INTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"확인된 수치"),
    re.compile(r"(?:관련\s*자료를\s*)?병렬\s*조회했습니다"),
    re.compile(r"전략\s*mart에서\s*조회했습니다", re.IGNORECASE),
    re.compile(r"해당\s*주장은\s*현재\s*근거\s*자격으로\s*확인되지\s*않았습니다"),
)
_INTERNAL_MART_PROGRESS_RE = re.compile(
    r"UBIST\s*전략\s*mart\s*지표\s*:.*(?:매출|MS|점유율|순위)",
    re.IGNORECASE,
)
_SEMANTIC_CLAUSE_SEPARATOR_RE = re.compile(
    r"\s*(?:,|;)\s*|(?:\s+)?(?:있으며|이며|이고|이지만|하지만|다만),?\s+"
)


def enforce_reason_codes(
    text: str,
    results: Sequence[SourceResult],
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply deterministic, clause-bounded R11 semantic repairs."""

    observed_at = (now or datetime.now(UTC)).date()
    repairs: list[tuple[str, str]] = []

    transfer = _repair_transfer(text, results)
    if transfer is not None:
        repairs.append(("UNSUPPORTED_TRANSFER_ATTRIBUTION", transfer))

    absence = _repair_absence(text, results)
    if absence is not None:
        repairs.append(("ABSENCE_OVERCLAIM", absence))

    dated = _repair_as_of_date(text, results, observed_at)
    if dated is not None:
        repairs.append(("AS_OF_DATE", dated))

    review_only = len(repairs) >= 2
    repaired = text if review_only else repairs[0][1] if repairs else text
    repaired, internal_count = scrub_internal_release_tokens(repaired)

    trace: dict[str, Any] = {code: 0 for code in _REASON_CODES}
    for code, _ in repairs:
        trace[code] += 1
    trace["INTERNAL_TOKEN_LEAK"] = internal_count
    trace["semantic_repair_candidates"] = len(repairs)
    trace["review_only"] = review_only
    return repaired, trace


def typed_absence_record(result: SourceResult) -> AbsenceConfirmation | None:
    if result.status != "empty" or not isinstance(result.payload, Mapping):
        return None
    raw = result.payload.get("absence_confirmation")
    if not isinstance(raw, Mapping):
        return None
    try:
        record = AbsenceConfirmation.model_validate(raw)
    except ValidationError:
        return None
    expected_source = {
        "reimbursement": "hira",
        "approval": "nedrug",
    }[record.doc_type]
    if record.source != result.source or record.source != expected_source:
        return None
    return record


def scrub_internal_release_tokens(text: str) -> tuple[str, int]:
    """Remove release-only prose without dropping adjacent grounded sentences."""

    count = 0
    repaired = text
    repaired, substitutions = _INTERNAL_PATTERNS[0].subn("", repaired)
    count += substitutions

    def remove_progress_sentence(sentence: str) -> str:
        nonlocal count
        kept: list[str] = []
        progress_patterns = (*_INTERNAL_PATTERNS[1:], _INTERNAL_MART_PROGRESS_RE)
        if not any(pattern.search(sentence) for pattern in progress_patterns):
            return sentence
        for clause in _SEMANTIC_CLAUSE_SEPARATOR_RE.split(sentence):
            normalized = clause.strip()
            if not normalized:
                continue
            matches = sum(len(pattern.findall(normalized)) for pattern in progress_patterns)
            if matches:
                count += matches
                continue
            kept.append(normalized)
        return _join_semantic_clauses(kept)

    repaired = _transform_sentences(repaired, remove_progress_sentence)
    repaired = re.sub(r"(?m)^\s*[.:;,·-]+\s*$", "", repaired)
    repaired = re.sub(r"[ \t]+([.,])", r"\1", repaired)
    repaired = re.sub(r"(?m)^\s*\.\s*", "", repaired)
    repaired = re.sub(r"\n{3,}", "\n\n", repaired)
    repaired = re.sub(r" {2,}", " ", repaired)
    return repaired.strip(), count


def _repair_transfer(
    text: str,
    results: Sequence[SourceResult],
) -> str | None:
    if not _TRANSFER_RE.search(text):
        return None
    contradictory = _TRANSFER_UNCERTAINTY_RE.search(text) is not None
    seen_observations: set[str] = set()
    repaired = _transform_sentences(
        text,
        lambda sentence: _repair_transfer_sentence(
            sentence,
            results,
            contradictory=contradictory,
            seen_observations=seen_observations,
        ),
    )
    return repaired if repaired != text else None


def _repair_transfer_sentence(
    sentence: str,
    results: Sequence[SourceResult],
    *,
    contradictory: bool,
    seen_observations: set[str],
) -> str:
    matches = _directed_transfer_matches(sentence)
    if not matches:
        if not _TRANSFER_RE.search(sentence):
            return sentence
        repaired_clauses: list[str] = []
        observation_inserted = False
        repair_applied = False
        for clause in _SEMANTIC_CLAUSE_SEPARATOR_RE.split(sentence):
            normalized = clause.strip()
            if not normalized:
                continue
            if not _UNDIRECTED_TRANSFER_OVERCLAIM_RE.search(
                normalized
            ) or _TRANSFER_BOUNDED_RE.search(normalized):
                repaired_clauses.append(normalized)
                continue
            if not observation_inserted:
                observation = _transfer_observation(results)
                if observation not in seen_observations:
                    repaired_clauses.append(observation)
                    seen_observations.add(observation)
                observation_inserted = True
            repair_applied = True
        return _join_semantic_clauses(repaired_clauses) if repair_applied else sentence

    output: list[str] = []
    cursor = 0
    for match in matches:
        output.append(sentence[cursor : match.start()])
        source_entity = match.group("from")
        target_entity = match.group("to")
        if not contradictory and _has_exact_flow(results, source_entity, target_entity):
            output.append(match.group(0))
        else:
            observation = _transfer_observation(
                results,
                source_entity=source_entity,
                target_entity=target_entity,
            )
            if observation not in seen_observations:
                output.append(observation)
                seen_observations.add(observation)
        cursor = match.end()
    output.append(sentence[cursor:])
    return "".join(output)


def _directed_transfer_matches(sentence: str) -> tuple[re.Match[str], ...]:
    candidates = sorted(
        (match for pattern in _DIRECTED_TRANSFER_PATTERNS for match in pattern.finditer(sentence)),
        key=lambda match: (match.start(), -(match.end() - match.start())),
    )
    selected: list[re.Match[str]] = []
    for match in candidates:
        if selected and match.start() < selected[-1].end():
            continue
        selected.append(match)
    return tuple(selected)


def _transfer_observation(
    results: Sequence[SourceResult],
    *,
    source_entity: str | None = None,
    target_entity: str | None = None,
) -> str:
    observation = symmetric_observation(
        results,
        entities=(source_entity, target_entity)
        if source_entity and target_entity
        else None,
    )
    if observation:
        return observation
    return (
        "서로 반대 방향의 변화가 관측됐습니다. 다만 환자·처방자 수준의 "
        "직접 이동 여부는 현재 자료로 확인되지 않습니다."
    )


def _has_exact_flow(
    results: Sequence[SourceResult],
    source_entity: str,
    target_entity: str,
) -> bool:
    expected = f"flow:{source_entity}->{target_entity}".casefold()
    return any(
        evidence is not None
        and any(value.casefold() == expected for value in evidence.eligible_attributions)
        for evidence in (result.evidence for result in results)
    )


def _repair_absence(
    text: str,
    results: Sequence[SourceResult],
) -> str | None:
    records = [record for result in results if (record := typed_absence_record(result))]
    if not _ABSENCE_CERTAINTY_RE.search(text):
        return None
    contradictory = _ABSENCE_UNCERTAINTY_RE.search(text) is not None
    repaired = _transform_sentences(
        text,
        lambda sentence: _repair_absence_sentence(
            sentence,
            records,
            contradictory=contradictory,
        ),
    )
    return repaired if repaired != text else None


def _repair_absence_sentence(
    sentence: str,
    records: Sequence[AbsenceConfirmation],
    *,
    contradictory: bool,
) -> str:
    if not _ABSENCE_CERTAINTY_RE.search(sentence):
        return sentence
    doc_type = "approval" if "허가" in sentence else "reimbursement"
    subject = _absence_subject(sentence, doc_type)
    matching = [record for record in records if record.doc_type == doc_type]
    if subject:
        matching = [
            record for record in matching if record.subject.casefold() == subject.casefold()
        ]
    elif len(matching) != 1:
        matching = []
    confirmed = any(record.status == "confirmed_non_reimbursed" for record in matching)
    if confirmed and not contradictory:
        return sentence
    if doc_type == "reimbursement":
        replacement = (
            "현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다. "
            "이 결과만으로 비급여 여부를 확정할 수는 없습니다."
        )
    else:
        public_subject = subject or (matching[0].subject if matching else "해당 제품")
        replacement = (
            f"현재 조회한 식품의약품안전처 자료에서는 {public_subject}의 허가 문서를 "
            "찾지 못했습니다. 이 결과만으로 허가 부재를 확정할 수는 없습니다."
        )
    return _replace_matching_clauses(
        sentence,
        _ABSENCE_CERTAINTY_RE,
        replacement,
    )


def _absence_subject(sentence: str, doc_type: str) -> str | None:
    marker = r"(?:급여\s*기준|비급여)" if doc_type == "reimbursement" else r"허가\s*문서"
    match = re.search(
        rf"(?P<subject>[0-9A-Za-z가-힣+_.-]{{2,40}}?)(?:은|는|이|가|의)?\s*"
        rf"(?:현재\s*)?{marker}",
        sentence,
    )
    return match.group("subject") if match is not None else None


def _repair_as_of_date(
    text: str,
    results: Sequence[SourceResult],
    observed_at: date,
) -> str | None:
    if not _FUTURE_DATE_RE.search(text):
        return None
    patent_date = _first_payload_date(results, "patent", ("patent_expiry", "expiry_date"))
    if patent_date is not None and patent_date < observed_at:
        replacement = (
            f"해당 특허의 {patent_date:%Y-%m-%d} 만료일은 이미 경과했습니다."
        )
        return _replace_matching_sentences(text, _FUTURE_DATE_RE, replacement)

    clinical_date = _first_payload_date(
        results,
        "clinicaltrials",
        ("start_date", "study_start_date"),
    )
    if clinical_date is not None and clinical_date < observed_at:
        status = _first_payload_value(
            results,
            "clinicaltrials",
            ("recruitment_status", "overall_status"),
        ) or "확인되지 않음"
        replacement = (
            f"시험 시작일은 {clinical_date:%Y-%m-%d}로 시작일이 도래했으며 "
            f"현재 모집상태는 {status}입니다."
        )
        return _replace_matching_sentences(text, _FUTURE_DATE_RE, replacement)
    return None


def _replace_matching_sentences(
    text: str,
    pattern: re.Pattern[str],
    replacement: str,
) -> str:
    inserted = False

    def replace(sentence: str) -> str:
        nonlocal inserted
        if not pattern.search(sentence):
            return sentence
        current_replacement = "" if inserted else replacement
        repaired = _replace_matching_clauses(
            sentence,
            pattern,
            current_replacement,
        )
        if current_replacement and repaired != sentence:
            inserted = True
        return repaired

    return _transform_sentences(text, replace)


def _replace_matching_clauses(
    sentence: str,
    pattern: re.Pattern[str],
    replacement: str,
) -> str:
    clauses = _SEMANTIC_CLAUSE_SEPARATOR_RE.split(sentence)
    kept: list[str] = []
    replacement_used = False
    for clause in clauses:
        normalized = clause.strip()
        if not normalized:
            continue
        if pattern.search(normalized):
            if replacement and not replacement_used:
                kept.append(replacement.strip())
                replacement_used = True
            continue
        kept.append(normalized)
    return _join_semantic_clauses(kept)


def _join_semantic_clauses(clauses: Sequence[str]) -> str:
    output = ""
    for clause in clauses:
        normalized = clause.strip()
        if not normalized:
            continue
        if not output:
            output = normalized
            continue
        separator = " " if output.endswith((".", "!", "?")) else ". "
        output = f"{output.rstrip(',; ')}{separator}{normalized}"
    return output.strip()


def _transform_sentences(
    text: str,
    transform: Any,
) -> str:
    parts = re.split(r"(?<=[.!?])(?=\s)|(?<=\n)", text)
    output: list[str] = []
    for part in parts:
        match = re.match(r"(?s)^(\s*)(.*?)(\s*)$", part)
        if match is None:
            output.append(part)
            continue
        leading, body, trailing = match.groups()
        output.append(leading + transform(body) + trailing)
    return "".join(output).strip()


def _first_payload_date(
    results: Sequence[SourceResult],
    source: str,
    keys: tuple[str, ...],
) -> date | None:
    value = _first_payload_value(results, source, keys)
    if not value:
        return None
    match = re.search(r"(\d{4})[-./](\d{1,2})(?:[-./](\d{1,2}))?", value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))
    except ValueError:
        return None


def _first_payload_value(
    results: Sequence[SourceResult],
    source: str,
    keys: tuple[str, ...],
) -> str | None:
    key_set = {key.casefold() for key in keys}
    for result in results:
        if result.source != source:
            continue
        for key, value in _walk_items(result.payload):
            if key.casefold() in key_set and value not in (None, ""):
                return str(value).strip()
    return None


def _walk_items(value: Any) -> Sequence[tuple[str, Any]]:
    if isinstance(value, Mapping):
        output: list[tuple[str, Any]] = []
        for key, item in value.items():
            output.append((str(key), item))
            output.extend(_walk_items(item))
        return tuple(output)
    if isinstance(value, (list, tuple)):
        return tuple(pair for item in value for pair in _walk_items(item))
    return ()
