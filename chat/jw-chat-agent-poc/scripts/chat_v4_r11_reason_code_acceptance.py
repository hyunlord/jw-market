from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Iterable

from jw_chat_agent_poc.service.v4.contracts import EvidenceEnvelope, SourceResult
from jw_chat_agent_poc.service.v4.reason_code_enforcement import enforce_reason_codes


OBSERVED_AT = datetime(2026, 8, 12, tzinfo=UTC)
REASON_CODES = (
    "UNSUPPORTED_TRANSFER_ATTRIBUTION",
    "ABSENCE_OVERCLAIM",
    "INTERNAL_TOKEN_LEAK",
    "AS_OF_DATE",
)
PREFIXES = tuple(
    f"기준 관측 {index}의 매출은 124.54억원입니다."
    for index in range(1, 11)
)
SUFFIXES = (
    "[출처: 내부 데이터마트]",
    "비교 기간은 동일합니다. [출처: 내부 데이터마트]",
    "단위는 억원입니다. [출처: 내부 데이터마트]",
    "시장 범위는 동일합니다. [출처: 내부 데이터마트]",
    "수치는 표시 정밀도를 유지했습니다. [출처: 내부 데이터마트]",
    "추가 해석은 별도 검증이 필요합니다. [출처: 내부 데이터마트]",
)


@dataclass(frozen=True, slots=True)
class AcceptanceCase:
    case_id: str
    reason_code: str
    expected_repair: bool
    text: str
    results: tuple[SourceResult, ...]


def _comparison_result() -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로젯과 리피토 매출 비교",
        status="ok",
        payload={
            "calls": [
                {
                    "entity_bundle": {
                        "anchor": "리바로젯",
                        "period_start": "2025-09",
                        "period_end": "2026-06",
                        "members": [
                            {
                                "brand": "리바로젯",
                                "role": "target",
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 111.00},
                                        {"period": "2026-06", "value_억원": 124.54},
                                    ]
                                },
                            },
                            {
                                "brand": "리피토",
                                "role": "competitor",
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 52.49},
                                        {"period": "2026-06", "value_억원": 39.00},
                                    ]
                                },
                            },
                        ],
                    }
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="mart",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            subject_grain="brand",
        ),
    )


def _absence_result(status: str = "doc_not_found") -> SourceResult:
    claims = (
        ("reimbursement", "absence_confirmation", "absence_confirmation:reimbursement")
        if status == "confirmed_non_reimbursed"
        else ("reimbursement",)
    )
    return SourceResult(
        source="hira",
        query="마운자로 급여기준",
        status="empty",
        payload={
            "absence_confirmation": {
                "source": "hira",
                "doc_type": "reimbursement",
                "status": status,
                "subject": "마운자로",
            }
        },
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=claims,
            causal=False,
            metric_type="document_absence",
            product=("마운자로",),
            subject_grain="brand",
        ),
    )


def _dated_result(source: str, value: str) -> SourceResult:
    if source == "patent":
        payload = {"items": [{"patent_expiry": value}]}
        claims = ("patent",)
        scope = "GLOBAL"
    else:
        payload = {
            "items": [
                {
                    "start_date": value,
                    "recruitment_status": "RECRUITING",
                }
            ]
        }
        claims = ("study_design", "recruitment_status")
        scope = "GLOBAL"
    return SourceResult(
        source=source,
        query="날짜 상태",
        status="ok",
        payload=payload,
        evidence=EvidenceEnvelope(
            kind=source,
            entity_match="EXACT",
            source_scope=scope,
            time_match="MATCH",
            eligible_claims=claims,
        ),
    )


def _decorated(template: str, prefix: str, suffix: str) -> str:
    return f"{prefix} {template} {suffix}"


def build_cases() -> tuple[AcceptanceCase, ...]:
    cases: list[AcceptanceCase] = []
    transfer_result = (_comparison_result(),)
    transfer_positive = (
        "리피토 감소분이 리바로젯으로 이동했습니다.",
        "리피토에서 리바로젯으로 전환됐습니다.",
        "리피토 -> 리바로젯 이동했습니다.",
        "리피토를 리바로젯으로 대체했습니다.",
    )
    transfer_negative = (
        "직접 이동 여부는 현재 자료로 확인되지 않습니다.",
        "브랜드 간 전환 여부는 추가 확인이 필요합니다.",
        "처방 대체를 단정할 근거는 부족합니다.",
        "브랜드 잠식 여부는 판단할 수 없습니다.",
    )
    cases.extend(
        _matrix_cases(
            "transfer-positive",
            "UNSUPPORTED_TRANSFER_ATTRIBUTION",
            True,
            transfer_positive,
            transfer_result,
        )
    )
    cases.extend(
        _matrix_cases(
            "transfer-negative",
            "UNSUPPORTED_TRANSFER_ATTRIBUTION",
            False,
            transfer_negative,
            transfer_result,
        )
    )

    absence_positive = (
        "마운자로는 비급여입니다.",
        "마운자로는 비급여로 확인됐습니다.",
        "마운자로 급여 기준이 없습니다.",
        "마운자로는 비급여로 분류됐습니다.",
    )
    absence_negative = (
        "이 결과만으로 비급여 여부를 확정할 수는 없습니다.",
        "현재 조회에서는 급여 상태가 확인되지 않았습니다.",
        "마운자로 급여기준은 추가 확인이 필요합니다.",
        "마운자로 급여 여부를 단정할 근거가 부족합니다.",
    )
    cases.extend(
        _matrix_cases(
            "absence-positive",
            "ABSENCE_OVERCLAIM",
            True,
            absence_positive,
            (_absence_result(),),
        )
    )
    cases.extend(
        _matrix_cases(
            "absence-negative",
            "ABSENCE_OVERCLAIM",
            False,
            absence_negative,
            (_absence_result(),),
        )
    )

    internal_positive = (
        "확인된 수치",
        "병렬 조회했습니다.",
        "관련 자료를 병렬 조회했습니다.",
        "전략 mart에서 조회했습니다.",
    )
    internal_negative = (
        "동일 기간 수치를 비교했습니다.",
        "여러 출처의 근거를 구분했습니다.",
        "수치와 해석을 분리했습니다.",
        "자료 범위의 한계를 함께 표시했습니다.",
    )
    cases.extend(
        _matrix_cases(
            "internal-positive",
            "INTERNAL_TOKEN_LEAK",
            True,
            internal_positive,
            (),
        )
    )
    cases.extend(
        _matrix_cases(
            "internal-negative",
            "INTERNAL_TOKEN_LEAK",
            False,
            internal_negative,
            (),
        )
    )

    past_patent = (_dated_result("patent", "2024-06-30"),)
    future_patent = (_dated_result("patent", "2027-06-30"),)
    as_of_positive = (
        "해당 특허는 2024년 6월 만료 예정입니다.",
        "해당 특허는 만료를 앞두고 있습니다.",
        "해당 특허 만료일이 다가오고 있습니다.",
        "2024년 특허 만료가 예정돼 있습니다.",
    )
    as_of_negative = (
        "해당 특허 만료일은 이미 경과했습니다.",
        "해당 특허의 현재 상태를 확인했습니다.",
        "해당 특허는 2027년 만료 예정입니다.",
        "향후 상태는 별도 확인이 필요합니다.",
    )
    cases.extend(
        _matrix_cases(
            "asof-positive",
            "AS_OF_DATE",
            True,
            as_of_positive,
            past_patent,
        )
    )
    cases.extend(
        _matrix_cases_mixed_results(
            "asof-negative",
            "AS_OF_DATE",
            False,
            as_of_negative,
            (past_patent, past_patent, future_patent, future_patent),
        )
    )
    return tuple(cases)


def _matrix_cases(
    prefix: str,
    reason_code: str,
    expected_repair: bool,
    templates: tuple[str, ...],
    results: tuple[SourceResult, ...],
) -> Iterable[AcceptanceCase]:
    for template_index, template in enumerate(templates, start=1):
        for prefix_index, lead in enumerate(PREFIXES, start=1):
            for suffix_index, tail in enumerate(SUFFIXES, start=1):
                yield AcceptanceCase(
                    case_id=f"{prefix}-{template_index:02d}-{prefix_index:02d}-{suffix_index:02d}",
                    reason_code=reason_code,
                    expected_repair=expected_repair,
                    text=_decorated(template, lead, tail),
                    results=results,
                )


def _matrix_cases_mixed_results(
    prefix: str,
    reason_code: str,
    expected_repair: bool,
    templates: tuple[str, ...],
    result_sets: tuple[tuple[SourceResult, ...], ...],
) -> Iterable[AcceptanceCase]:
    for template_index, (template, results) in enumerate(
        zip(templates, result_sets, strict=True),
        start=1,
    ):
        yield from _matrix_cases(
            f"{prefix}-{template_index:02d}",
            reason_code,
            expected_repair,
            (template,),
            results,
        )


def evaluate_cases() -> tuple[list[dict[str, object]], dict[str, dict[str, float | int]]]:
    rows: list[dict[str, object]] = []
    for case in build_cases():
        output, trace = enforce_reason_codes(
            case.text,
            case.results,
            now=OBSERVED_AT,
        )
        observed = int(trace[case.reason_code]) > 0
        rows.append(
            {
                "case_id": case.case_id,
                "reason_code": case.reason_code,
                "expected_repair": case.expected_repair,
                "observed_repair": observed,
                "correct": observed == case.expected_repair,
                "grounded_value_preserved": "124.54억원" in output,
                "source_preserved": "[출처: 내부 데이터마트]" in output,
                "nonempty": bool(output.strip()),
                "input": case.text,
                "output": output,
            }
        )

    summary: dict[str, dict[str, float | int]] = {}
    for reason_code in REASON_CODES:
        selected = [row for row in rows if row["reason_code"] == reason_code]
        true_positive = sum(
            bool(row["expected_repair"] and row["observed_repair"])
            for row in selected
        )
        false_positive = sum(
            bool(not row["expected_repair"] and row["observed_repair"])
            for row in selected
        )
        false_negative = sum(
            bool(row["expected_repair"] and not row["observed_repair"])
            for row in selected
        )
        predicted_positive = true_positive + false_positive
        actual_positive = true_positive + false_negative
        summary[reason_code] = {
            "samples": len(selected),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": true_positive / predicted_positive if predicted_positive else 1.0,
            "recall": true_positive / actual_positive if actual_positive else 1.0,
            "grounded_value_loss": sum(
                not bool(row["grounded_value_preserved"]) for row in selected
            ),
            "source_loss": sum(not bool(row["source_preserved"]) for row in selected),
            "empty_output": sum(not bool(row["nonempty"]) for row in selected),
        }
    return rows, summary


def main() -> None:
    rows, summary = evaluate_cases()
    print("case_id\treason_code\texpected_repair\tobserved_repair\tcorrect\tgrounded_value_preserved\tsource_preserved\tnonempty\tinput\toutput")
    for row in rows:
        print(
            "\t".join(
                str(row[key]).replace("\t", " ").replace("\n", "\\n")
                for key in (
                    "case_id",
                    "reason_code",
                    "expected_repair",
                    "observed_repair",
                    "correct",
                    "grounded_value_preserved",
                    "source_preserved",
                    "nonempty",
                    "input",
                    "output",
                )
            )
        )
    print("# SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
