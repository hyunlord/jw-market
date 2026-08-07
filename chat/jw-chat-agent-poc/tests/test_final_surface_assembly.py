from __future__ import annotations

from copy import deepcopy

import pytest

from jw_chat_agent_poc.orchestrator import final_surface_assembly
from jw_chat_agent_poc.orchestrator.final_surface_assembly import apply_final_surface_assembly
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryEntity,
    QueryOperation,
    RequestQuerySpec,
)
from jw_chat_agent_poc.service import app as service_app


SOURCE = """## 출처

| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 | 브랜드 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-05 | 전략뷰 | 리바로 리바로젯 | 555 | 전체 | 억원 | 리바로 |"""


def _spec(
    operation: QueryOperation,
    *,
    brands: tuple[str, ...],
    metrics: tuple[str, ...],
) -> RequestQuerySpec:
    entities = tuple(QueryEntity(EntityKind.BRAND, brand, brand) for brand in brands)
    return RequestQuerySpec(
        entities=entities,
        operation=operation,
        metrics=metrics,
        comparison_targets=entities if operation is QueryOperation.COMPARE_CURRENT else (),
    )


def test_current_value_keeps_requested_scalar_and_source_but_omits_long_series() -> None:
    answer = f"""리바로의 최근 흐름을 분석했습니다.

### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 80.39억원 |
| 시장점유율 | 3.76% |
| 순위 | 6/555 |

### 분석 기준별 점유율
| 순위 | 구분 | MS | 매출 |
| --- | --- | --- | --- |
| 1 | 로수젯 | 9.13% | 195.24억원 |

**리바로 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-08 | 79.63억원 | 3.93% |
| 2026-05 | 80.39억원 | 3.76% |

{SOURCE}"""
    markdown_response = {"fact_md": "매출 fact 80.39억원\n과거 fact 79.63억원"}
    before = deepcopy(markdown_response)

    result = apply_final_surface_assembly(
        "리바로 매출 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("리바로",), metrics=("sales",)),
        markdown_response=markdown_response,
    )

    assert result.answer.startswith("리바로의 2026-05 매출은 80.39억원입니다.")
    assert "| 매출 | 80.39억원 |" in result.answer
    assert "시장점유율" not in result.answer
    assert "매출 시계열" not in result.answer
    assert "분석 기준별 점유율" not in result.answer
    assert SOURCE in result.answer
    assert result.actions == ("concise_current_value",)
    assert markdown_response == before


def test_current_value_keeps_requested_source_measurement_basis() -> None:
    notice = "원외 처방(UBIST) 기준으로 답합니다."
    answer = f"""{notice}

### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 80.39억원 |

**리바로 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 84.93억원 | 3.75% |
| 2026-05 | 80.39억원 | 3.76% |

{SOURCE}"""

    result = apply_final_surface_assembly(
        "리바로 UBIST 매출 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("리바로",), metrics=("sales",)),
    )

    assert result.answer.startswith("리바로의 2026-05 매출은 80.39억원입니다.")
    assert notice in result.answer


def test_current_value_with_two_metrics_keeps_both_requested_values() -> None:
    answer = f"""## 전략뷰

### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 195.24억원 |
| 시장점유율 | 9.13% |
| 순위 | 1/555 |

**로수젯 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 206.85억원 | 9.14% |
| 2026-05 | 195.24억원 | 9.13% |

{SOURCE}"""

    result = apply_final_surface_assembly(
        "로수젯 매출과 시장점유율 알려줘",
        answer,
        _spec(
            QueryOperation.CURRENT_VALUE,
            brands=("로수젯",),
            metrics=("sales", "share"),
        ),
    )

    assert result.answer.startswith(
        "로수젯의 2026-05 매출은 195.24억원이고 시장점유율은 9.13%입니다."
    )
    assert "| 시장점유율 | 9.13% |" in result.answer
    assert "206.85억원" not in result.answer


def test_current_value_can_select_latest_values_when_only_a_series_is_rendered() -> None:
    answer = f"""## 전략뷰 (market_landscape)

**로수젯 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 206.85억원 | 9.14% |
| 2026-05 | 195.24억원 | 9.13% |

## 일반뷰 (ATC4)

- 로수젯: 점유율 13.99%, 매출 195.2억원

{SOURCE}"""

    result = apply_final_surface_assembly(
        "로수젯 매출과 시장점유율 알려줘",
        answer,
        _spec(
            QueryOperation.CURRENT_VALUE,
            brands=("로수젯",),
            metrics=("sales", "share"),
        ),
    )

    assert result.answer.startswith(
        "로수젯의 2026-05 매출은 195.24억원이고 시장점유율은 9.13%입니다."
    )
    assert "## 일반뷰" not in result.answer
    assert "206.85억원" not in result.answer


def test_current_value_closes_source_before_excluded_extra_view_but_keeps_notices() -> None:
    answer = f"""**로수젯 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 206.85억원 | 9.14% |
| 2026-05 | 195.24억원 | 9.13% |

- 요청한 분석에 필요한 출처(IQVIA NSA)를 현재 조회할 수 없습니다.

{SOURCE}

## 일반뷰 (ATC4)

- 로수젯: 점유율 13.99%, 매출 195.2억원

근거 정합을 확인하지 못한 항목은 제외합니다."""

    result = apply_final_surface_assembly(
        "로수젯 매출과 시장점유율 알려줘",
        answer,
        _spec(
            QueryOperation.CURRENT_VALUE,
            brands=("로수젯",),
            metrics=("sales", "share"),
        ),
    )

    assert "## 일반뷰" not in result.answer
    assert "13.99%" not in result.answer
    assert "요청한 분석에 필요한 출처" in result.answer
    assert "근거 정합을 확인하지 못한 항목" in result.answer


def test_current_value_preserves_safety_sections_and_validation_notices() -> None:
    answer = f"""### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 80.39억원 |

**리바로 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 84.93억원 | 3.75% |
| 2026-05 | 80.39억원 | 3.76% |

- 숫자 검증: 일부 기간의 단위를 확인할 수 없습니다.

## 주의

확인되지 않은 값은 추정하지 않았습니다.

{SOURCE}"""

    result = apply_final_surface_assembly(
        "리바로 매출 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("리바로",), metrics=("sales",)),
    )

    assert "## 주의" in result.answer
    assert "확인되지 않은 값은 추정하지 않았습니다." in result.answer
    assert "숫자 검증: 일부 기간의 단위를 확인할 수 없습니다." in result.answer


def test_compare_current_puts_existing_values_into_a_clear_lead_without_delta_math() -> None:
    answer = f"""## 브랜드 비교
| 브랜드 | 시작 점유율 | 최신 점유율 | 방향 | 시작 매출 | 최신 매출 |
| --- | --- | --- | --- | --- | --- |
| 리바로젯 | 근거 불일치로 제외 | 2026-05 5.12% | 상승 | 95.50억원 | 109.46억원 |
| 리바로 | 근거 불일치로 제외 | 2026-05 3.76% | 하락 | 79.63억원 | 80.39억원 |

{SOURCE}"""

    result = apply_final_surface_assembly(
        "리바로와 리바로젯 매출 비교해줘",
        answer,
        _spec(
            QueryOperation.COMPARE_CURRENT,
            brands=("리바로", "리바로젯"),
            metrics=("sales",),
        ),
    )

    assert result.answer.startswith(
        "최신 매출은 리바로젯 109.46억원, 리바로 80.39억원입니다."
    )
    assert "29.07" not in result.answer
    assert "## 브랜드 비교" in result.answer
    assert result.actions == ("lead_current_comparison",)


def test_positioning_moves_existing_direct_conclusion_first_and_drops_series_dump() -> None:
    answer = f"""경쟁 구도를 분석했습니다.

| 순위 | 브랜드 | 점유율 | 매출 |
| --- | --- | --- | --- |
| 1위 | 로수젯 | 9.13% | 195.24억원 |
| 5위 | 로수바미브 | 4.20% | 89.76억원 |

**리바로 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2021-06 | 67.47억원 | 4.69% |
| 2026-05 | 80.39억원 | 3.76% |

## 포지셔닝 축
| 축 | 자사/관찰 값 | 시장 상위 대비 위치 |
| --- | --- | --- |
| 시장 순위/MS | 리바로 2026-05 시장점유율 3.76% 순위 6 | 보유 근거 범위 |

자사 위치: 리바로 6위, 시장점유율 3.76%. 직상위 5위 로수바미브(4.20%)와 격차 0.44%p입니다.

## MI implication
| 관찰 | 가능한 의미 | 확인 필요 |
| --- | --- | --- |
| 시장 순위 | 기준점 | 반복 관측 |

{SOURCE}"""

    result = apply_final_surface_assembly(
        "리바로 경쟁 상대는 누구고 우리 위치는 어디야?",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("리바로",), metrics=()),
    )

    assert result.answer.startswith(
        "자사 위치: 리바로 6위, 시장점유율 3.76%. 직상위 5위 로수바미브(4.20%)와 격차 0.44%p입니다."
    )
    assert "| 1위 | 로수젯 |" in result.answer
    assert "## 포지셔닝 축" in result.answer
    assert "매출 시계열" not in result.answer
    assert "## MI implication" not in result.answer
    assert result.actions == ("prioritize_positioning_conclusion",)


def test_hira_criteria_keeps_summary_and_source_but_omits_raw_page_dump() -> None:
    answer = """만 12세 이상은 최근 24주간 출혈 건수가 6회 이상이면 급여가 인정됩니다.

투여 후 6개월마다 평가하며 ABR이 5 이상이면 중지합니다.

아래는 건강보험심사평가원에서 제공하는 보험인정기준 상세 고시 원문입니다.
---
< 건강보험심사평가원 보험인정기준 상세내용 인쇄
Emicizumab 주사제
게시일 2023-05-01 조회수 901
■ 관련 문헌
1. Hematology: Basic Principles and Practice

## 출처
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 | 브랜드 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 심사평가원(HIRA) 보험인정기준 | 2023-05 | 해당 없음 | 해당 없음 | 해당 없음 | 전체 | % | 해당 없음 |"""

    result = apply_final_surface_assembly(
        "헴리브라 급여기준 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("헴리브라",), metrics=()),
    )

    assert "최근 24주간 출혈 건수가 6회" in result.answer
    assert "ABR이 5 이상" in result.answer
    assert "심사평가원(HIRA) 보험인정기준" in result.answer
    assert "상세 고시 원문입니다" not in result.answer
    assert "상세내용 인쇄" not in result.answer
    assert "관련 문헌" not in result.answer
    assert result.actions == ("omit_hira_raw_page_dump",)


def test_hira_raw_page_without_horizontal_rule_is_also_omitted() -> None:
    answer = """주요 질환별 투여 대상 및 평가 기준은 다음과 같습니다.

아래는 건강보험심사평가원에서 제공하는 상세 고시 원문입니다.
건강보험심사평가원 보험인정기준 상세내용 인쇄
Tocilizumab 주사제
## 주의사항
원문 페이지의 긴 주의사항 본문입니다.
■ 고시 개정 전체내용

## 출처
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 | 브랜드 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 심사평가원(HIRA) 보험인정기준 | 2026-04 | 해당 없음 | 해당 없음 | 해당 없음 | 전체 | % | 해당 없음 |"""

    result = apply_final_surface_assembly(
        "악템라 급여기준 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("악템라",), metrics=()),
    )

    assert result.answer.startswith("주요 질환별 투여 대상 및 평가 기준은 다음과 같습니다.")
    assert "Tocilizumab" not in result.answer
    assert "상세 고시 원문" not in result.answer
    assert "## 주의사항" not in result.answer
    assert "원문 페이지의 긴 주의사항" not in result.answer


def test_already_concise_answer_is_byte_identical() -> None:
    answer = f"리바로의 2026-05 매출은 80.39억원입니다.\n\n{SOURCE}"

    result = apply_final_surface_assembly(
        "리바로 매출 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("리바로",), metrics=("sales",)),
    )

    assert result.answer == answer
    assert result.actions == ()


def test_surface_assembly_fails_open_on_internal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = "기존 답변 바이트"
    monkeypatch.setattr(
        final_surface_assembly,
        "_assemble",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )

    result = apply_final_surface_assembly(
        "리바로 매출 알려줘",
        answer,
        _spec(QueryOperation.CURRENT_VALUE, brands=("리바로",), metrics=("sales",)),
    )

    assert result.answer == answer
    assert result.actions == ()
    assert result.failed_open is True


def test_compute_final_answer_applies_surface_assembly_at_the_delivery_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = f"""### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 80.39억원 |

**리바로 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 84.93억원 | 3.75% |
| 2026-05 | 80.39억원 | 3.76% |

{SOURCE}"""
    monkeypatch.setattr(
        service_app,
        "_compute_final_answer",
        lambda *_args, **_kwargs: service_app.FinalAnswer(
            text=answer,
            charts=[],
            timing={},
            trace={},
            sources=("UBIST",),
            conversation_id="g4-delivery",
        ),
    )
    monkeypatch.setattr(service_app, "trace_envelope", lambda **_kwargs: {})

    final = service_app.compute_final_answer(
        "리바로 매출 알려줘",
        {"markdown_response": {"fact_md": "매출 fact 80.39억원"}, "tool_calls": []},
        query_spec=_spec(
            QueryOperation.CURRENT_VALUE,
            brands=("리바로",),
            metrics=("sales",),
        ),
    )

    assert final.text.startswith("리바로의 2026-05 매출은 80.39억원입니다.")
    assert "84.93억원" not in final.text


def test_compute_final_answer_preserves_typed_partial_byte_for_query_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = f"""### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 80.39억원 |

**리바로 매출 시계열**
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-04 | 84.93억원 | 3.75% |
| 2026-05 | 80.39억원 | 3.76% |

요청한 파일 facet은 확인할 수 없어 부분 결과만 제공합니다.

{SOURCE}"""
    monkeypatch.setattr(
        service_app,
        "_compute_final_answer",
        lambda *_args, **_kwargs: service_app.FinalAnswer(
            text=answer,
            charts=[],
            timing={},
            trace={},
            sources=("UBIST",),
            conversation_id="g4-typed-partial",
        ),
    )
    monkeypatch.setattr(service_app, "trace_envelope", lambda **_kwargs: {})
    result = {
        "partial_evidence": {
            "reason_code": "PARTIAL_EVIDENCE",
            "producer": "unresolvable_facet",
            "user_message": "요청한 파일 facet은 확인할 수 없어 부분 결과만 제공합니다.",
        },
        "markdown_response": {"fact_md": "매출 fact 80.39억원"},
        "tool_calls": [],
    }

    final = service_app.compute_final_answer(
        "리바로 매출 알려줘",
        result,
        query_spec=_spec(
            QueryOperation.CURRENT_VALUE,
            brands=("리바로",),
            metrics=("sales",),
        ),
    )

    assert final.text == answer
