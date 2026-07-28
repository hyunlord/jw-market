"""A missing source must read as a measurement-basis difference, not as an absent brand.

A market carries only the sources its own definition is built on. When the BQ plan reports
one of them missing, the existing notice says the source cannot be queried, which reads as
an outage or as the brand being absent from that vendor's data. These contracts pin the
added explanation, and pin that the existing verdict wording is untouched.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.loop import (
    _bq_source_label,
    _SOURCE_BASIS_LABEL,
    _SOURCE_DOMAIN_NOTE,
    _source_domain_note,
)
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder


def _verdict_notice(missing: tuple[str, ...]) -> str:
    """The wording loop.py emits first; reproduced here to pin that it is unchanged."""
    labels = ", ".join(_bq_source_label(source) for source in missing)
    return f"요청한 분석에 필요한 출처({labels})를 현재 조회할 수 없습니다."


# (a) the case under repair: one side of the pair missing.


def test_missing_iqvia_explains_the_measurement_basis() -> None:
    note = _source_domain_note(("iqvia_nsa",))
    assert note is not None
    assert "원외 처방(UBIST) 기준으로 정의돼 있습니다" in note
    assert "제조사 출하(IQVIA NSA) 기준과는" in note
    assert "측정 대상이 다른" in note
    for between in ("유통 재고", "병원 직거래", "반품", "원내 처방"):
        assert between in note


def test_missing_ubist_inverts_the_two_bases() -> None:
    """The note is about source nature, so it must read correctly either way round."""
    note = _source_domain_note(("ubist",))
    assert note is not None
    assert "제조사 출하(IQVIA NSA) 기준으로 정의돼 있습니다" in note
    assert "원외 처방(UBIST) 기준과는" in note


# (b) and (d): no note where none is warranted.


@pytest.mark.parametrize(
    "missing",
    [
        (),                          # (b) market carries both sources - nothing missing
        ("iqvia_nsa", "ubist"),      # both sides gone: the basis sentence would be a guess
        ("nsa",),                    # (c) an unknown label must not be narrated
        ("source_absent",),          # (c) M's reason code is not a source label
    ],
)
def test_no_note_when_the_basis_cannot_be_stated(missing: tuple[str, ...]) -> None:
    assert _source_domain_note(missing) is None


# the existing verdict wording is a contract; the note is additive.


def test_existing_verdict_wording_is_preserved_verbatim() -> None:
    assert _verdict_notice(("iqvia_nsa",)) == "요청한 분석에 필요한 출처(IQVIA NSA)를 현재 조회할 수 없습니다."
    note = _source_domain_note(("iqvia_nsa",))
    assert note is not None
    assert note != _verdict_notice(("iqvia_nsa",))
    assert "조회할 수 없습니다" not in note


def test_note_is_appended_after_the_verdict_not_merged_into_it() -> None:
    missing = ("iqvia_nsa",)
    notices = [_verdict_notice(missing)]
    note = _source_domain_note(missing)
    assert note is not None
    notices.append(note)
    assert notices[0] == "요청한 분석에 필요한 출처(IQVIA NSA)를 현재 조회할 수 없습니다."
    assert len(notices) == 2


# wording constraints that the surrounding machinery depends on.


def test_note_carries_no_digits() -> None:
    """verify_markdown_numbers scans the whole answer.

    A number token in the notice that no evidence backs would flip the response into its
    verification-failed branch, which rewrites the interpretation block.
    """
    note = _source_domain_note(("iqvia_nsa",))
    assert note is not None
    assert not any(character.isdigit() for character in note)


def test_note_never_says_the_data_is_absent() -> None:
    note = _source_domain_note(("iqvia_nsa",))
    assert note is not None
    for absent_phrasing in ("없습니다", "없음", "없다", "미보유", "존재하지"):
        assert absent_phrasing not in note


def test_note_hardcodes_no_market_or_brand() -> None:
    """The explanation is about what each source measures, not about one market."""
    for forbidden in ("ml_", "리바로", "아일리아", "C10", "스타틴"):
        assert forbidden not in _SOURCE_DOMAIN_NOTE
        for label in _SOURCE_BASIS_LABEL.values():
            assert forbidden not in label


def test_single_template_not_copied_per_source() -> None:
    """One constant, substituted - so the two directions cannot drift apart."""
    assert "{available}" in _SOURCE_DOMAIN_NOTE
    assert "{missing}" in _SOURCE_DOMAIN_NOTE
    assert set(_SOURCE_BASIS_LABEL) == {"ubist", "iqvia_nsa"}


# the note has to survive rendering, not just exist.


def test_note_reaches_the_rendered_answer_body() -> None:
    missing = ("iqvia_nsa",)
    note = _source_domain_note(missing)
    assert note is not None
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[],
        sources=["UBIST"],
        notices=[_verdict_notice(missing), note],
    )
    assert "측정 대상이 다른" in response.notice_md
    assert "측정 대상이 다른" in response.markdown
    assert "요청한 분석에 필요한 출처(IQVIA NSA)를 현재 조회할 수 없습니다." in response.markdown
    assert response.verification["status"] == "pass"
