"""F4 — brand_unresolved 가 두 등록 지점에 등재되었는지 지킨다.

LKUP(factory.py) 이 sources=["brand_unresolved"] 를 심는데, 이 값이
app.py 의 terminal typed 판별 집합과 source_display 의 라벨 사전 어디에도
등재되지 않아 두 가지가 함께 깨졌다:

* typed 사유("어느 브랜드 기준인지 확인되지 않아…")가 채택되지 않고 버려져
  LLM 이 본문을 새로 쓰면서 "데이터가 없다"는 사실과 다른 결론을 만들었다.
* 출처 라벨이 없어 공개 계층이 "—" 로 치환했다.

등록 누락은 조용히 실패한다. 그래서 값의 존재 자체를 테스트로 고정한다.
"""

from jw_chat_agent_poc.common.source_display import SOURCE_LABELS, public_source_label
from jw_chat_agent_poc.service.app import _is_terminal_typed_result

# ANCH-4 가 확인한, 이 회차 이전부터 등재되어 있던 값들. 불변이어야 한다.
PREEXISTING_TERMINAL_SOURCES = (
    "unsupported_brand",
    "ambiguous_brand",
    "strategic_market_not_member",
    "unsupported_hira_interface",
    "field_not_exposed",
)


def test_brand_unresolved_is_terminal_typed_result() -> None:
    # Given: LKUP 이 브랜드 미해소 시 심는 result 모양 (factory.py 의 sources 그대로)
    result = {"sources": ["brand_unresolved"]}

    # Then: terminal typed 로 판별되어야 답변 경로가 typed 사유를 그대로 쓴다.
    assert _is_terminal_typed_result(result) is True


def test_brand_unresolved_has_source_label() -> None:
    # Given/Then: 라벨이 등재되어 있어야 공개 계층이 "—" 로 치환하지 않는다.
    label = SOURCE_LABELS.get("brand_unresolved")
    assert label is not None
    assert label.strip() != ""
    # public_source_label 도 원문 식별자를 그대로 노출하지 않는다.
    assert public_source_label("brand_unresolved") == label
    assert "brand_unresolved" not in label


def test_preexisting_terminal_sources_are_unchanged() -> None:
    # 이 회차는 추가만 했다. 기존 값의 판별 결과가 바뀌면 추가가 아니라 변경이다.
    for source in PREEXISTING_TERMINAL_SOURCES:
        assert _is_terminal_typed_result({"sources": [source]}) is True


def test_preexisting_source_labels_are_unchanged() -> None:
    # 라벨을 가진 기존 항목의 문구가 바뀌지 않았는지 고정한다.
    assert SOURCE_LABELS["unsupported_brand"] == "브랜드 식별 미확인"
    assert SOURCE_LABELS["ambiguous_brand"] == "브랜드 식별 후보"
    assert SOURCE_LABELS["strategic_market_not_member"] == "전략시장 정의 미포함"


def test_unregistered_source_is_still_not_terminal() -> None:
    # 판별이 "무엇이든 통과"로 넓어지지 않았는지 확인한다(∅ ⊆ S 계열 방어).
    assert _is_terminal_typed_result({"sources": ["definitely_not_a_typed_source"]}) is False
    assert _is_terminal_typed_result({"sources": []}) is False
    assert _is_terminal_typed_result({"sources": ["brand_unresolved", "UBIST"]}) is False
