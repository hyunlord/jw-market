from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from jw_chat_agent_poc.service.v4.comparison_facts import (
    build_comparison_facts,
    symmetric_observation,
)
from jw_chat_agent_poc.service.v4.contracts import (
    AbsenceConfirmation,
    SourceResult,
)
from jw_chat_agent_poc.service.v4.time_context import (
    current_kst_date,
    datetime_kst_date,
)


_REASON_CODES = (
    "UNSUPPORTED_TRANSFER_ATTRIBUTION",
    "ABSENCE_OVERCLAIM",
    "INTERNAL_TOKEN_LEAK",
    "AS_OF_DATE",
    "PATENT_STATUS_OVERCLAIM",
)
_TRANSFER_RE = re.compile(r"(?:이동|흡수|전환|잠식|대체)")
_STRUCTURAL_COMMERCIAL_RE = re.compile(
    r"(?:매출|점유율|비중|성장|하락|감소|증가|처방|환자군|수요)"
)
_STRUCTURAL_RELATION_RE = re.compile(
    r"(?:원인|영향|연관|때문|결과|기인|견인|압박|야기|유발|"
    r"대체|이동|전환|잠식|흡수|유입|이어)"
)
_STRUCTURAL_HYPOTHESIS_RE = re.compile(
    r"(?:(?:가능성|가설)[^.\n]{0,30}(?:제기|시사|해석|설명)|"
    r"(?:제기|시사|해석|설명)[^.\n]{0,30}(?:가능성|가설))"
)
_STRUCTURAL_TARGET_FIRST_RE = re.compile(
    r"(?:성장|증가|상승)[^.\n]{0,100}(?:감소|하락|변화)[^.\n]{0,60}"
    r"(?:의\s*)?(?:결과|기인|때문|영향|가능성|가설|해석|설명)"
)
_ENTITY_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "리바로": ("리바로", "livalo", "pitavastatin", "피타바스타틴"),
    "리바로젯": ("리바로젯", "livalozet"),
    "리피토": ("리피토", "lipitor", "atorvastatin", "아토르바스타틴"),
    "아토젯": ("아토젯", "atozet"),
    "로수젯": ("로수젯", "rosuzet"),
    "크레스토": ("크레스토", "crestor", "rosuvastatin", "로수바스타틴"),
}
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
_COMMERCIAL_PRESCRIPTION_TRANSFER_RE = re.compile(
    r"(?:처방[^.\n]{0,40}(?:이동|흡수|전환|잠식|대체)[^.\n]{0,40}"
    r"(?:시장|트렌드|추세)|"
    r"(?:시장|트렌드|추세)[^.\n]{0,40}처방[^.\n]{0,40}"
    r"(?:이동|흡수|전환|잠식|대체)|"
    r"(?:매출|점유율)[^.\n]{0,100}처방(?:\s*수요|\s*량)?[^.\n]{0,40}"
    r"(?:이동|흡수|전환|잠식|대체)|"
    r"처방\s*수요[^.\n]{0,40}(?:이동|흡수|전환|잠식|대체))"
)
_CLINICAL_PRESCRIPTION_STUDY_RE = re.compile(
    r"(?:임상(?:시험|\s*연구)|clinical\s+trial)[^.\n]{0,120}"
    r"(?:평가|분석|비교)",
    re.IGNORECASE,
)
_ABSENCE_CERTAINTY_RE = re.compile(
    r"(?:비급여(?:입니다|로\s*확인|로\s*분류|에\s*해당)|"
    r"급여\s*기준이?\s*없습니다|허가\s*문서가?\s*없습니다)"
)
_NONREIMBURSED_MARKET_RE = re.compile(r"비급여\s*시장")
_ABSENCE_UNCERTAINTY_RE = re.compile(
    r"(?:비급여|급여|허가)[^.\n]{0,50}(?:확인되지|미확인|확정할\s*수\s*없)|"
    r"(?:확인되지|미확인|확정할\s*수\s*없)[^.\n]{0,50}(?:비급여|급여|허가)"
)
_PATENT_GLOBAL_EXPIRY_RE = re.compile(
    r"[^.\n]*특허(?:들|들은|는|가)?[^.\n]{0,100}"
    r"(?:(?:모두|전부|전체)[^.\n]{0,30})?(?:이미\s*)?소멸[^.\n]*[.]?"
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
_HIRA_UI_PREFIX_RE = re.compile(
    r"<?\s*건강보험심사평가원\s*보험인정기준\s*상세내용\s*인쇄"
    r".*?(?=■\s*고시\s*개정\s*전체내용)",
    re.DOTALL,
)
_HIRA_ATTACHMENT_UI_RE = re.compile(
    r"첨부파일\s*다운로드\s*자료가\s*다운되지\s*않을\s*경우"
    r".*?(?:확인해\s*주세요\.?\s*)?(?:닫기\s*)?$",
    re.DOTALL,
)
_HIRA_CLOSE_UI_RE = re.compile(r"\s*닫기\s*$")
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

    observed_at = datetime_kst_date(now) if now is not None else current_kst_date()
    repairs: list[tuple[str, str]] = []

    transfer = _repair_transfer(text, results)
    if transfer is not None:
        repairs.append(("UNSUPPORTED_TRANSFER_ATTRIBUTION", transfer))

    absence = _repair_absence(text, results)
    if absence is not None:
        repairs.append(("ABSENCE_OVERCLAIM", absence))

    patent_status = _repair_patent_status(text, results)
    if patent_status is not None:
        repairs.append(("PATENT_STATUS_OVERCLAIM", patent_status))

    dated = _repair_as_of_date(text, results, observed_at)
    if dated is not None:
        repairs.append(("AS_OF_DATE", dated))

    review_only = len(repairs) >= 2
    if review_only:
        repaired = patent_status if patent_status is not None else text
    else:
        repaired = repairs[0][1] if repairs else text
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


def _repair_patent_status(
    text: str,
    results: Sequence[SourceResult],
) -> str | None:
    if (
        "이번 조회 범위" in text
        or _PATENT_GLOBAL_EXPIRY_RE.search(text) is None
    ):
        return None
    records: list[Mapping[str, Any]] = []
    for result in results:
        if result.source != "patent" or not isinstance(result.payload, Mapping):
            continue
        lanes = result.payload.get("patent_lanes")
        if not isinstance(lanes, Mapping):
            continue
        kr_lane = lanes.get("kr_primary")
        if not isinstance(kr_lane, Mapping):
            continue
        raw_records = kr_lane.get("records")
        if isinstance(raw_records, list):
            records.extend(record for record in raw_records if isinstance(record, Mapping))
    if not records:
        return None
    registered = sum(
        str(record.get("status") or record.get("listed_status") or "").strip()
        == "등록"
        for record in records
    )
    replacement = (
        f"이번 조회 범위에서는 등록 상태 등재특허 {registered}건이 확인되었습니다."
        if registered
        else f"이번 조회에서 확인된 등재특허 {len(records)}건은 모두 소멸 상태입니다."
    )
    replaced = False

    def replace_once(_match: re.Match[str]) -> str:
        nonlocal replaced
        if replaced:
            return ""
        replaced = True
        return replacement

    return _PATENT_GLOBAL_EXPIRY_RE.sub(replace_once, text)


def scrub_internal_release_tokens(text: str) -> tuple[str, int]:
    """Remove release-only prose without dropping adjacent grounded sentences."""

    count = 0
    repaired = text
    repaired, substitutions = _HIRA_UI_PREFIX_RE.subn("", repaired)
    count += substitutions
    repaired, substitutions = _HIRA_ATTACHMENT_UI_RE.subn("", repaired)
    count += substitutions
    repaired, substitutions = _HIRA_CLOSE_UI_RE.subn("", repaired)
    count += substitutions
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
    has_structural_candidate = any(
        _structural_transfer_entities(sentence, results) is not None
        for sentence in re.split(r"(?<=[.!?])(?=\s)|(?<=\n)", text)
    )
    if not _TRANSFER_RE.search(text) and not has_structural_candidate:
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
    if (
        seen_observations
        and (
            "반대 방향의 변화가 관측됐습니다" in sentence
            or "직접 이동 여부는 현재 자료로 확인되지 않습니다" in sentence
        )
    ):
        return ""
    structural_entities = _structural_transfer_entities(sentence, results)
    if structural_entities is not None:
        source_entity, target_entity = structural_entities
        if _has_exact_flow(results, source_entity, target_entity):
            return sentence
        observation = _transfer_observation(
            results,
            source_entity=source_entity,
            target_entity=target_entity,
        )
        if observation in seen_observations:
            return ""
        seen_observations.add(observation)
        return observation

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
            commercial_prescription_transfer = (
                _COMMERCIAL_PRESCRIPTION_TRANSFER_RE.search(normalized)
                and not _CLINICAL_PRESCRIPTION_STUDY_RE.search(normalized)
            )
            unsupported = _UNDIRECTED_TRANSFER_OVERCLAIM_RE.search(
                normalized
            ) or commercial_prescription_transfer
            if not unsupported or _TRANSFER_BOUNDED_RE.search(normalized):
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


def _structural_transfer_entities(
    sentence: str,
    results: Sequence[SourceResult],
) -> tuple[str, str] | None:
    if not _STRUCTURAL_COMMERCIAL_RE.search(sentence):
        return None
    if not (
        _STRUCTURAL_RELATION_RE.search(sentence)
        or _STRUCTURAL_HYPOTHESIS_RE.search(sentence)
    ):
        return None
    mentions = _entity_mentions(sentence, results)
    distinct: list[str] = []
    for canonical in mentions:
        if canonical not in distinct:
            distinct.append(canonical)
    if len(distinct) < 2:
        return None
    if _STRUCTURAL_TARGET_FIRST_RE.search(sentence):
        return distinct[1], distinct[0]
    return distinct[0], distinct[1]


def _entity_mentions(
    sentence: str,
    results: Sequence[SourceResult],
) -> tuple[str, ...]:
    aliases: dict[str, str] = {}
    for canonical, values in _ENTITY_ALIAS_GROUPS.items():
        for value in values:
            aliases[value.casefold()] = canonical
    for result in results:
        if result.source != "mart":
            continue
        facts = build_comparison_facts((result,))
        for item in facts.get("brand_deltas", []):
            if not isinstance(item, Mapping):
                continue
            brand = str(item.get("brand") or "").strip()
            if brand:
                aliases.setdefault(brand.casefold(), brand)

    lowered = sentence.casefold()
    matches: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for alias, canonical in sorted(aliases.items(), key=lambda item: -len(item[0])):
        for match in re.finditer(re.escape(alias), lowered):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            occupied.append(span)
            matches.append((span[0], span[1], canonical))
    matches.sort()
    return tuple(canonical for _, _, canonical in matches)


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
    expected_source = _canonical_entity(source_entity)
    expected_target = _canonical_entity(target_entity)
    for evidence in (result.evidence for result in results):
        if evidence is None:
            continue
        for value in evidence.eligible_attributions:
            match = re.fullmatch(r"flow:(.+?)->(.+)", value, flags=re.IGNORECASE)
            if match is None:
                continue
            if (
                _canonical_entity(match.group(1)) == expected_source
                and _canonical_entity(match.group(2)) == expected_target
            ):
                return True
    return False


def _canonical_entity(value: str) -> str:
    folded = value.strip().casefold()
    for canonical, aliases in _ENTITY_ALIAS_GROUPS.items():
        if folded == canonical.casefold() or folded in {
            alias.casefold() for alias in aliases
        }:
            return canonical
    return value.strip()


def _repair_absence(
    text: str,
    results: Sequence[SourceResult],
) -> str | None:
    records = [record for result in results if (record := typed_absence_record(result))]
    if not _ABSENCE_CERTAINTY_RE.search(text) and not (
        records and _NONREIMBURSED_MARKET_RE.search(text)
    ):
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
    certainty_pattern = (
        _ABSENCE_CERTAINTY_RE
        if _ABSENCE_CERTAINTY_RE.search(sentence)
        else _NONREIMBURSED_MARKET_RE
    )
    if not certainty_pattern.search(sentence) or (
        certainty_pattern is _NONREIMBURSED_MARKET_RE and not records
    ):
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
        certainty_pattern,
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
            f"시험 시작일은 {clinical_date:%Y-%m-%d}로 시작일이 도래했습니다. "
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
