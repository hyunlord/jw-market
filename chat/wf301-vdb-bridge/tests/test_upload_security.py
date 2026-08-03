from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import fitz
from fastapi import UploadFile

from src import models, upload_adapter


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def _pptx_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<presentation />")
    return stream.getvalue()


def _docx_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", "<document />")
    return stream.getvalue()


def _pdf_bytes() -> bytes:
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "harmless validation fixture")
        return document.tobytes()


def test_validation_fails_closed_when_whitelist_is_empty() -> None:
    errors = upload_adapter.validate_extensions(
        [_upload("report.pdf", _pdf_bytes())], frozenset()
    )

    assert errors == ["허용된 파일 확장자 설정을 확인할 수 없습니다."]


def test_validation_preserves_explicit_local_format_allowlist() -> None:
    errors = upload_adapter.validate_extensions(
        [_upload("report.docx", _docx_bytes())], frozenset({"pdf", "ppt", "pptx"})
    )

    assert errors == []


def test_validation_rejects_extension_outside_all_allowlists() -> None:
    errors = upload_adapter.validate_extensions(
        [_upload("report.exe", b"MZ harmless-test-fixture")],
        frozenset({"pdf", "ppt", "pptx"}),
    )

    assert errors == ["허용되지 않는 파일 확장자입니다: report.exe"]


def test_validation_rejects_dangerous_inner_extension() -> None:
    errors = upload_adapter.validate_extensions(
        [_upload("quarterly.php.pdf", _pdf_bytes())], frozenset({"pdf"})
    )

    assert errors == ["위험한 이중 확장자 파일명입니다: quarterly.php.pdf"]


def test_validation_rejects_hidden_dangerous_inner_extension() -> None:
    errors = upload_adapter.validate_extensions(
        [_upload(".PHP.pdf", _pdf_bytes())], frozenset({"pdf"})
    )

    assert errors == ["위험한 이중 확장자 파일명입니다: .PHP.pdf"]


def test_validation_rejects_path_bearing_filename() -> None:
    errors = upload_adapter.validate_extensions(
        [_upload("../report.pdf", _pdf_bytes())], frozenset({"pdf"})
    )

    assert errors == ["안전하지 않은 파일명입니다: report.pdf"]


def test_validation_rejects_pdf_with_executable_signature() -> None:
    upload = _upload("quarterly.pdf", b"\x7fELF harmless-test-fixture")

    errors = upload_adapter.validate_extensions([upload], frozenset({"pdf"}))

    assert errors == ["파일 내용이 확장자와 일치하지 않습니다: quarterly.pdf"]
    assert upload.file.tell() == 0


def test_validation_accepts_pdf_signature_and_restores_stream() -> None:
    upload = _upload("quarterly.v2.pdf", _pdf_bytes())

    assert upload_adapter.validate_extensions([upload], frozenset({"pdf"})) == []
    assert upload.file.tell() == 0


def test_validation_accepts_legacy_ppt_signature() -> None:
    content = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        + b" harmless fixture "
        + "PowerPoint Document".encode("utf-16le")
    )

    assert upload_adapter.validate_extensions(
        [_upload("slides.ppt", content)], frozenset({"ppt"})
    ) == []


def test_validation_rejects_bare_ole_header_as_ppt() -> None:
    content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 harmless fixture"

    errors = upload_adapter.validate_extensions(
        [_upload("slides.ppt", content)], frozenset({"ppt"})
    )

    assert errors == ["파일 내용이 확장자와 일치하지 않습니다: slides.ppt"]


def test_validation_accepts_pptx_container() -> None:
    assert upload_adapter.validate_extensions(
        [_upload("slides.pptx", _pptx_bytes())], frozenset({"pptx"})
    ) == []


def test_validation_rejects_zip_without_matching_ooxml_structure() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/not-a-presentation.xml", "<document />")

    errors = upload_adapter.validate_extensions(
        [_upload("slides.pptx", stream.getvalue())], frozenset({"pptx"})
    )

    assert errors == ["파일 내용이 확장자와 일치하지 않습니다: slides.pptx"]


def test_saved_temp_document_is_not_executable(tmp_path) -> None:
    saved = upload_adapter.save_temp_documents(
        [_upload("report.pdf", _pdf_bytes())],
        temp_document_ids=[7],
        destination_dir=tmp_path,
    )

    mode = stat.S_IMODE((tmp_path / "TEMP_DOCUMENT_7.pdf").stat().st_mode)
    assert mode == 0o644
    assert mode & 0o111 == 0
    assert saved[0].file_path.endswith("TEMP_DOCUMENT_7.pdf")


def test_public_upload_response_does_not_reflect_file_content() -> None:
    marker = "UNTRUSTED_SCRIPT_MARKER"
    projected = models.PublicUploadResponse.model_validate(
        {
            "temp_documents": [
                {
                    "temp_document_id": 7,
                    "file_name": "report.pdf",
                    "file_path": "/private/report.pdf",
                    "file_content": marker,
                }
            ],
            "file_content": marker,
        }
    ).model_dump_json()

    assert marker not in projected


def test_upload_route_checks_quota_before_parsing_file_content() -> None:
    source = (Path(__file__).parents[1] / "src" / "main.py").read_text()

    assert source.index("if not quota.allowed:") < source.index("validate_extensions(")
