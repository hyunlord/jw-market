from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.upload_status import (
    UploadFileCard,
    UploadFileStatus,
    UploadJobNotFoundError,
    UploadStatusRegistry,
    UploadWorksheetCard,
)


def test_upload_status_persists_across_registry_instances(tmp_path: Path) -> None:
    registry = UploadStatusRegistry(tmp_path)
    job = registry.create(
        session_id="session-a",
        workflow_id=301,
        file_names=("wide.xlsx", "report.pdf"),
        file_cards=(
            UploadFileCard(
                file_name="wide.xlsx",
                file_type="xlsx",
                size_bytes=123,
                sheet_count=1,
                sheets=(UploadWorksheetCard("Raw", 12_269, 252),),
            ),
            UploadFileCard(
                file_name="report.pdf",
                file_type="pdf",
                size_bytes=456,
                page_count=185,
            ),
        ),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    registry.transition(
        session_id="session-a",
        workflow_id=301,
        upload_id=job.upload_id,
        state="preprocessing",
        files=(
            UploadFileStatus("wide.xlsx", state="preprocessing"),
            UploadFileStatus("report.pdf", state="preprocessing"),
        ),
    )

    reloaded = UploadStatusRegistry(tmp_path).resolve(
        session_id="session-a",
        workflow_id=301,
        upload_id=job.upload_id,
    )

    assert reloaded.state == "preprocessing"
    assert [item.file_name for item in reloaded.files] == ["wide.xlsx", "report.pdf"]
    assert reloaded.files[0].card is not None
    assert reloaded.files[0].card.sheets == (
        UploadWorksheetCard("Raw", 12_269, 252),
    )
    assert reloaded.files[1].card is not None
    assert reloaded.files[1].card.page_count == 185
    assert registry.status_path("session-a", job.upload_id).stat().st_mode & 0o777 == 0o600


def test_upload_status_hides_jobs_from_other_sessions(tmp_path: Path) -> None:
    registry = UploadStatusRegistry(tmp_path)
    job = registry.create(
        session_id="session-a",
        workflow_id=301,
        file_names=("wide.xlsx",),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    with pytest.raises(UploadJobNotFoundError, match="upload job is not registered"):
        registry.resolve(
            session_id="session-b",
            workflow_id=301,
            upload_id=job.upload_id,
        )


def test_stale_nonterminal_upload_becomes_interrupted(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    registry = UploadStatusRegistry(tmp_path, stale_after=timedelta(minutes=5))
    job = registry.create(
        session_id="session-a",
        workflow_id=301,
        file_names=("wide.xlsx",),
        expires_at=now + timedelta(days=1),
        now=now - timedelta(minutes=10),
    )

    interrupted = registry.resolve(
        session_id="session-a",
        workflow_id=301,
        upload_id=job.upload_id,
        now=now,
    )

    assert interrupted.state == "interrupted"
    assert interrupted.ready is False
    assert interrupted.message == "파일 처리가 중단되었습니다. 다시 업로드해 주세요."


def test_upload_status_rejects_untrusted_identifier(tmp_path: Path) -> None:
    registry = UploadStatusRegistry(tmp_path)

    with pytest.raises(UploadJobNotFoundError, match="upload job is not registered"):
        registry.resolve(
            session_id="session-a",
            workflow_id=301,
            upload_id="../../other-session",
        )
