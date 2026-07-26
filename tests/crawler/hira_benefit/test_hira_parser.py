from __future__ import annotations

import pytest

from pipeline.scripts.crawler.hira_benefit import parser as hira_parser
from pipeline.scripts.crawler.hira_benefit.models import FieldParseStatus, ParseStatus
from pipeline.scripts.crawler.hira_benefit.parser import (
    parse_detail_html,
    parse_list_html,
)
from pipeline.scripts.crawler.hira_benefit.typed_extraction import parse_stored_raw_text

LIST_HTML = """
<table>
  <tr>
    <td>약제</td>
    <td><a href="/rc/drug/insuadtcrtr/bbsView.do?brdBltNo=53026&amp;brdScnBltNo=4">
      [약제] 고시 제2025-189호 안내
    </a></td>
    <td>2025-11-28</td>
  </tr>
  <tr>
    <td>약제</td>
    <td><a href="/rc/drug/insuadtcrtr/bbsView.do?brdBltNo=52929">
      고시 제2025-100호 안내
    </a></td>
    <td>2025.06.27</td>
  </tr>
</table>
"""


def test_list_parser_extracts_notice_identity_date_and_absolute_url() -> None:
    rows = parse_list_html(LIST_HTML, base_url="https://www.hira.or.kr")

    assert [row.source_notice_id for row in rows] == ["53026", "52929"]
    assert rows[0].notice_date.isoformat() == "2025-11-28"
    assert rows[0].title == "[약제] 고시 제2025-189호 안내"
    assert rows[0].source_url.startswith("https://www.hira.or.kr/rc/")
    assert len(rows[0].listing_fingerprint) == 64


def test_list_parser_extracts_current_hira_popup_identity() -> None:
    html = """
    <table><tbody><tr>
      <td class="col-gubun">고시</td>
      <td class="col-num2">고시 제2026-133호 (약제)</td>
      <td class="col-tit"><a href="#none"
        onclick="viewInsuAdtCrtr(3, '20260701', '1', '0005', '3'); return false;">
        Zastaprazan 경구제(품명: 자큐보정20밀리그램 등)
      </a></td>
      <td class="col-date">2026-07-01</td>
    </tr></tbody></table>
    """

    rows = parse_list_html(html, base_url="https://www.hira.or.kr")

    assert len(rows) == 1
    assert rows[0].source_notice_id == "20260701-1-0005"
    assert rows[0].notice_date.isoformat() == "2026-07-01"
    assert rows[0].title.startswith("Zastaprazan")
    assert rows[0].source_url == (
        "https://www.hira.or.kr/rc/insu/insuadtcrtr/"
        "InsuAdtCrtrPopup.do?mtgHmeDd=20260701&sno=1&mtgMtrRegSno=0005"
    )


def test_detail_parser_marks_ok_only_when_all_structured_fields_exist() -> None:
    html = """
    <main>
      <h1>보험인정기준 상세내용</h1>
      <div class="title">[약제] 고시 제2025-189호 안내</div>
      <dl><dt>관련근거</dt><dd>고시 제2025-189호</dd>
          <dt>게시일</dt><dd>2025-11-28</dd></dl>
      <h2>투여대상</h2><p>성인 중 LDL-C 조절이 필요한 환자</p>
      <h2>제외기준</h2><p>중증 간장애 환자는 제외한다.</p>
      <h2>투여용량</h2><p>1일 1회 1정, 최대 12개월</p>
    </main>
    """

    parsed = parse_detail_html(
        html,
        source_notice_id="53026",
        source_url="https://www.hira.or.kr/detail?brdBltNo=53026",
    )

    assert parsed.parse_status is ParseStatus.OK
    assert parsed.notice_no == "제2025-189호"
    assert parsed.notice_date.isoformat() == "2025-11-28"
    assert "LDL-C" in (parsed.target_condition or "")
    assert "간장애" in (parsed.exclusion_rule or "")
    assert "12개월" in (parsed.dosage_limit or "")
    assert parsed.failed_fields == ()


def test_detail_parser_preserves_raw_text_and_marks_optional_fields_applicable() -> None:
    html = """
    <main><h1>보험인정기준 상세내용</h1>
    <div class="title">고시 제2026-10호</div><p>게시일 2026-01-03</p>
    <h2>투여대상</h2><p>특정 환자군</p></main>
    """

    parsed = parse_detail_html(
        html,
        source_notice_id="60000",
        source_url="https://www.hira.or.kr/detail?brdBltNo=60000",
    )

    assert parsed.parse_status is ParseStatus.OK
    assert parsed.target_condition == "특정 환자군"
    assert parsed.exclusion_rule is None
    assert parsed.dosage_limit is None
    assert parsed.failed_fields == ()
    assert "특정 환자군" in parsed.raw_text


def test_detail_parser_marks_failed_without_synthesizing_values() -> None:
    parsed = parse_detail_html(
        "<html><body><p>첨부파일에서 세부 기준을 확인하십시오.</p></body></html>",
        source_notice_id="60001",
        source_url="https://www.hira.or.kr/detail?brdBltNo=60001",
    )

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.target_condition is None
    assert parsed.exclusion_rule is None
    assert parsed.dosage_limit is None
    assert parsed.failed_fields == ("ingress:missing_expected_structure",)
    assert parsed.raw_text == "첨부파일에서 세부 기준을 확인하십시오."


@pytest.mark.parametrize("html", ("", " \n\t "))
def test_detail_parser_rejects_empty_ingress(html: str) -> None:
    parsed = parse_detail_html(
        html,
        source_notice_id="empty-ingress",
        source_url="https://www.hira.or.kr/detail?brdBltNo=empty-ingress",
    )

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.failed_fields == ("ingress:empty_raw_text",)


def test_detail_parser_rejects_unclosed_structural_html() -> None:
    parsed = parse_detail_html(
        "<main><h1>보험인정기준 상세내용</h1><p>정상 본문",
        source_notice_id="broken-html",
        source_url="https://www.hira.or.kr/detail?brdBltNo=broken-html",
    )

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.failed_fields == ("ingress:malformed_html",)


def test_detail_parser_rejects_missing_expected_heading() -> None:
    parsed = parse_detail_html(
        "<html><body><main><h1>일반 게시물</h1><p>정상 본문</p></main></body></html>",
        source_notice_id="missing-structure",
        source_url="https://www.hira.or.kr/detail?brdBltNo=missing-structure",
    )

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.failed_fields == ("ingress:missing_expected_structure",)


def test_detail_parser_rejects_missing_content_container() -> None:
    parsed = parse_detail_html(
        "<html><h1>보험인정기준 상세내용</h1><p>정상처럼 보이는 본문</p></html>",
        source_notice_id="missing-container",
        source_url="https://www.hira.or.kr/detail?brdBltNo=missing-container",
    )

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.failed_fields == ("ingress:missing_expected_structure",)


def test_detail_parser_rejects_http_error_page_body() -> None:
    parsed = parse_detail_html(
        "<html><head><title>Internal Server Error</title></head>"
        "<body><p>HTTP Status 500</p></body></html>",
        source_notice_id="http-error",
        source_url="https://www.hira.or.kr/detail?brdBltNo=http-error",
    )

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.failed_fields == ("ingress:http_error_page",)


def test_detail_parser_does_not_extract_fields_after_ingress_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(**_kwargs: object) -> None:
        pytest.fail("typed extraction must not run after ingress failure")

    monkeypatch.setattr(hira_parser, "extract_structured", fail_if_called)

    parsed = parse_detail_html(
        "<html><body><p>HTTP Status 500</p></body></html>",
        source_notice_id="no-extraction",
        source_url="https://www.hira.or.kr/detail?brdBltNo=no-extraction",
    )

    assert parsed.parse_status is ParseStatus.FAILED


def test_detail_parser_keeps_normal_document_without_typed_clauses_not_applicable() -> None:
    parsed = parse_detail_html(
        "<main><h1>보험인정기준 상세내용</h1>"
        "<p>요양급여의 적용기준 및 방법에 대한 세부사항을 개정한다.</p></main>",
        source_notice_id="normal-no-clauses",
        source_url="https://www.hira.or.kr/detail?brdBltNo=normal-no-clauses",
    )

    assert parsed.parse_status is ParseStatus.NOT_APPLICABLE
    assert parsed.failed_fields == ()


def test_stored_raw_text_ignores_common_attachment_download_boilerplate() -> None:
    parsed = parse_stored_raw_text(
        "본문내용.pdf 첨부파일 다운로드 자료가 다운되지 않을 경우 담당부서로 "
        "연락주시기 바랍니다. 첨부파일명이 한글로 되어있는 경우 다운로드시 "
        "확인해 주세요. 국민건강보험 요양급여 적용기준을 개정한다. 닫기"
    )

    assert parsed.parse_status is ParseStatus.NOT_APPLICABLE
    assert parsed.failed_fields == ()


def test_stored_raw_text_does_not_treat_fee_or_material_phrases_as_parse_failures() -> None:
    fee = parse_stored_raw_text(
        "제2의 것부터는 50%를 산정하되 최대 200%까지 산정한다. 닫기"
    )
    material = parse_stored_raw_text(
        "별도로 산정하도록 명시된 경우를 제외하고 소정점수를 산정한다. 닫기"
    )

    assert fee.parse_status is ParseStatus.NOT_APPLICABLE
    assert fee.failed_fields == ()
    assert material.parse_status is ParseStatus.NOT_APPLICABLE
    assert material.failed_fields == ()


def test_stored_raw_text_marks_empty_structural_label_as_failed() -> None:
    parsed = parse_stored_raw_text("보험인정기준 상세내용 1. 투여 대상:")

    assert parsed.parse_status is ParseStatus.FAILED
    assert parsed.failed_fields == ("target_condition",)


def test_detail_parser_does_not_treat_common_h1_as_target_condition() -> None:
    html = """
    <main>
      <h1>보험인정기준 상세내용</h1>
      <div class="title">[약제] 고시 제2025-169호 안내</div>
      <p>요양급여의 적용기준 및 방법에 대한 세부사항을 개정한다.</p>
    </main>
    """

    parsed = parse_detail_html(
        html,
        source_notice_id="52991",
        source_url="https://www.hira.or.kr/detail?brdBltNo=52991",
    )

    assert parsed.target_condition is None
    assert parsed.exclusion_rule is None
    assert parsed.dosage_limit is None
    assert parsed.parse_status is ParseStatus.NOT_APPLICABLE
    assert parsed.failed_fields == ()


def test_detail_parser_extracts_typed_fields_from_table_rows() -> None:
    html = """
    <main>
      <h1>보험인정기준 상세내용</h1>
      <table>
        <tr><th>투여대상</th><td>성인 중 LDL-C 조절이 필요한 환자</td></tr>
        <tr><th>제외기준</th><td>중증 간장애 환자는 제외한다.</td></tr>
        <tr><th>투여용량</th><td>1일 1회 1정, 최대 12개월</td></tr>
      </table>
    </main>
    """

    parsed = parse_detail_html(
        html,
        source_notice_id="table-1",
        source_url="https://www.hira.or.kr/detail?brdBltNo=table-1",
    )

    assert parsed.parse_status is ParseStatus.OK
    assert parsed.target_condition == "성인 중 LDL-C 조절이 필요한 환자"
    assert parsed.exclusion_rule == "중증 간장애 환자는 제외한다."
    assert parsed.dosage_limit == "1일 1회 1정, 최대 12개월"


def test_detail_parser_extracts_typed_fields_from_numbered_paragraphs() -> None:
    html = """
    <main>
      <h1>보험인정기준 상세내용</h1>
      <p>1. 투여대상: 재발성 환자</p>
      <p>2. 제외기준: 임신부는 제외한다.</p>
      <p>가. 투여용량: 4주마다 1회 투여한다.</p>
    </main>
    """

    parsed = parse_detail_html(
        html,
        source_notice_id="numbered-1",
        source_url="https://www.hira.or.kr/detail?brdBltNo=numbered-1",
    )

    assert parsed.parse_status is ParseStatus.OK
    assert parsed.target_condition == "재발성 환자"
    assert parsed.exclusion_rule == "임신부는 제외한다."
    assert parsed.dosage_limit == "4주마다 1회 투여한다."


def test_stored_raw_text_reparse_requires_structure_before_marking_failure() -> None:
    parsed = parse_stored_raw_text(
        "보험인정기준 상세내용 1. 투여대상: 특정 환자군 "
        "2. 제외기준: 투여 금기 환자 3. 투여용량: 1일 1회"
    )
    not_applicable = parse_stored_raw_text(
        "보험인정기준 상세내용 요양급여의 적용기준 및 방법을 개정한다."
    )
    free_prose = parse_stored_raw_text(
        "보험인정기준 상세내용 임신부는 투여 대상에서 제외한다."
    )

    assert parsed.parse_status is ParseStatus.OK
    assert parsed.target_condition == "특정 환자군"
    assert parsed.exclusion_rule == "투여 금기 환자"
    assert parsed.dosage_limit == "1일 1회"
    assert not_applicable.parse_status is ParseStatus.NOT_APPLICABLE
    assert not_applicable.failed_fields == ()
    assert free_prose.parse_status is ParseStatus.NOT_APPLICABLE
    assert free_prose.failed_fields == ()


def test_stored_raw_text_uses_numbered_sibling_boundaries() -> None:
    parsed = parse_stored_raw_text(
        "보험인정기준 상세내용 - 다 음 - "
        "가. 투여대상 분별함수 점수가 32점 이상인 중증 알코올성 간염환자 "
        "나. 투여용량 및 기간 1회 400mg을 1일 3회, 최대 4주 이내 "
        "* 시행일: 2013.9.1. * 변경사유: 용어정비 닫기"
    )

    assert parsed.target_condition == (
        "분별함수 점수가 32점 이상인 중증 알코올성 간염환자"
    )
    assert parsed.dosage_limit == "1회 400mg을 1일 3회, 최대 4주 이내"
    assert parsed.parse_status is ParseStatus.OK


def test_stored_raw_text_parses_numbered_labels_without_colons() -> None:
    parsed = parse_stored_raw_text(
        "투여기준 1) 투여대상 아래 조건을 모두 충족하는 경우 "
        "가) 체온이 38도 이상인 경우 나) 호흡수가 기준을 넘는 경우 "
        "2) 용법 및 용량 하루 1회, 최대 14일 투여 "
        "■ 고시 개정 사유 대상 범위 명확화 닫기"
    )

    assert parsed.target_condition == (
        "아래 조건을 모두 충족하는 경우 "
        "가) 체온이 38도 이상인 경우 나) 호흡수가 기준을 넘는 경우"
    )
    assert parsed.dosage_limit == "하루 1회, 최대 14일 투여"
    assert parsed.parse_status is ParseStatus.OK


def test_stored_raw_text_parses_verified_label_variants() -> None:
    parsed = parse_stored_raw_text(
        "가. 투여 대상: 성인 환자 "
        "나. 인정용량: 수술 당 최대 500ml "
        "다. 제외 대상: 임신부 닫기"
    )
    compact_dosage = parse_stored_raw_text("1. 용법용량: 1일 1회")
    generic_dosage = parse_stored_raw_text("1. 용량: 최대 5mg")

    assert parsed.target_condition == "성인 환자"
    assert parsed.dosage_limit == "수술 당 최대 500ml"
    assert parsed.exclusion_rule == "임신부"
    assert parsed.parse_status is ParseStatus.OK
    assert compact_dosage.dosage_limit == "1일 1회"
    assert generic_dosage.dosage_limit == "최대 5mg"


def test_stored_raw_text_parses_exclusion_label_with_qualifier() -> None:
    parsed = parse_stored_raw_text(
        "가. 급여대상 반복적 진통제를 사용하는 환자 "
        "나. 급여제외 대상(금기증) 1) 출혈경향이 있는 경우 "
        "2) 임신을 한 경우 "
        "다. 사전검사 초음파촬영으로 결석을 확인한다. 닫기"
    )

    assert parsed.target_condition == "반복적 진통제를 사용하는 환자"
    assert parsed.exclusion_rule == (
        "1) 출혈경향이 있는 경우 2) 임신을 한 경우"
    )
    assert parsed.parse_status is ParseStatus.OK


def test_stored_raw_text_extracts_only_structural_contraindication_labels() -> None:
    contraindicated_patients = parse_stored_raw_text(
        "2. 금기환자 가. 활동성 결핵 환자 나. 중증 심부전 환자 "
        "3. 교체투여 다른 약제로 교체한다. 닫기"
    )
    contraindications = parse_stored_raw_text(
        "다. 금기증은 아래와 같으며 요양급여를 인정하지 아니함. "
        "1) 기대 여명 1년 이하 2) 활동성 심내막염 "
        "라. 시설 기준 관련 기준을 충족해야 한다. 닫기"
    )

    assert contraindicated_patients.exclusion_rule == (
        "가. 활동성 결핵 환자 나. 중증 심부전 환자"
    )
    assert contraindications.exclusion_rule == (
        "은 아래와 같으며 요양급여를 인정하지 아니함. "
        "1) 기대 여명 1년 이하 2) 활동성 심내막염"
    )


def test_stored_raw_text_does_not_promote_free_prose_denials_to_exclusion() -> None:
    material = parse_stored_raw_text(
        "치료재료 비용 중 재료대를 제외한다. 닫기"
    )
    combination = parse_stored_raw_text(
        "다른 약제와 병용투여는 급여로 인정하지 아니함. 닫기"
    )

    assert material.exclusion_rule is None
    assert material.exclusion_status is FieldParseStatus.NOT_APPLICABLE
    assert combination.exclusion_rule is None
    assert combination.exclusion_status is FieldParseStatus.NOT_APPLICABLE


def test_field_status_distinguishes_extracted_absent_and_failed() -> None:
    parsed = parse_stored_raw_text(
        "1. 투여대상: 성인 환자 2. 제외기준: 3. 투여용량: 1일 1회"
    )

    assert parsed.target_status is FieldParseStatus.EXTRACTED
    assert parsed.exclusion_status is FieldParseStatus.FAILED
    assert parsed.dosage_status is FieldParseStatus.EXTRACTED
    assert parsed.parse_status is ParseStatus.PARTIAL
    assert parsed.failed_fields == ("exclusion_rule",)


def test_field_status_marks_absent_labels_not_applicable() -> None:
    parsed = parse_stored_raw_text("보험인정기준 상세내용을 개정한다.")

    assert parsed.target_status is FieldParseStatus.NOT_APPLICABLE
    assert parsed.exclusion_status is FieldParseStatus.NOT_APPLICABLE
    assert parsed.dosage_status is FieldParseStatus.NOT_APPLICABLE
    assert parsed.parse_status is ParseStatus.NOT_APPLICABLE
