from __future__ import annotations

from docx import Document

from src import docx_preprocessor, main


def test_docx_chunk_records_preserve_heading_sections(tmp_path) -> None:
    path = tmp_path / "report.docx"
    document = Document()
    document.add_heading("Executive summary", level=1)
    document.add_paragraph("Revenue increased while share remained flat.")
    document.add_heading("Safety", level=1)
    document.add_paragraph("No new safety signal was identified.")
    document.save(path)

    records = docx_preprocessor.extract_docx_chunk_records(path)

    assert [record.section_title for record in records] == [
        "Executive summary",
        "Safety",
    ]
    assert "Revenue increased" in records[0].text
    assert "No new safety signal" in records[1].text


def test_context_projects_docx_section_and_pptx_slide_locations() -> None:
    context, sources, empty_pages = main._context_from_hits(
        [
            {
                "text": "No new safety signal was identified.",
                "summary": '{"source_channel":"native_text","section_title":"Safety"}',
                "doc_id": 7,
                "file_name": "report.docx",
                "i_page": 1,
                "i_chunk_on_doc": 2,
                "_additional": {"id": "docx-2", "distance": 0.1},
            },
            {
                "text": "The market outlook remains positive.",
                "summary": "{}",
                "doc_id": 8,
                "file_name": "brief.pptx",
                "i_page": 12,
                "i_chunk_on_doc": 11,
                "_additional": {"id": "pptx-12", "distance": 0.2},
            },
        ]
    )

    assert empty_pages == []
    assert "section=Safety" in context
    assert "slide=12" in context
    assert sources[0].section_title == "Safety"
    assert sources[0].slide_number is None
    assert sources[1].section_title is None
    assert sources[1].slide_number == 12
