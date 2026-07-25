from __future__ import annotations

from pipeline.scripts.crawler.hira_benefit.models import ParseStatus
from pipeline.scripts.crawler.hira_benefit.parser import (
    parse_detail_html,
    parse_list_html,
)

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


def test_detail_parser_marks_ok_only_when_all_structured_fields_exist() -> None:
    html = """
    <main>
      <h1>[약제] 고시 제2025-189호 안내</h1>
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


def test_detail_parser_preserves_raw_text_and_marks_partial() -> None:
    html = """
    <main><h1>고시 제2026-10호</h1><p>게시일 2026-01-03</p>
    <h2>투여대상</h2><p>특정 환자군</p></main>
    """

    parsed = parse_detail_html(
        html,
        source_notice_id="60000",
        source_url="https://www.hira.or.kr/detail?brdBltNo=60000",
    )

    assert parsed.parse_status is ParseStatus.PARTIAL
    assert parsed.target_condition == "특정 환자군"
    assert parsed.exclusion_rule is None
    assert parsed.dosage_limit is None
    assert parsed.failed_fields == ("exclusion_rule", "dosage_limit")
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
    assert parsed.failed_fields == (
        "target_condition",
        "exclusion_rule",
        "dosage_limit",
    )
    assert parsed.raw_text == "첨부파일에서 세부 기준을 확인하십시오."
