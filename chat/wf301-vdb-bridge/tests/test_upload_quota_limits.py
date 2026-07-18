from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src import settings
from src.main import _quota_snapshot, app


MIB = 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings_without_quota_env() -> dict[str, int]:
    env = os.environ.copy()
    env.pop("QUOTA_MAX_FILE_MB", None)
    env.pop("QUOTA_MAX_SESSION_MB", None)
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            (
                "import json; from src import settings; "
                "print(json.dumps({'file': settings.QUOTA_MAX_FILE_MB, "
                "'session': settings.QUOTA_MAX_SESSION_MB}))"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
    )
    return json.loads(output)


def test_default_upload_limits_are_100_mib_and_remain_env_configurable() -> None:
    assert _settings_without_quota_env() == {"file": 100, "session": 100}

    env = os.environ.copy()
    env["QUOTA_MAX_FILE_MB"] = "71"
    env["QUOTA_MAX_SESSION_MB"] = "93"
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            (
                "import json; from src import settings; "
                "print(json.dumps({'file': settings.QUOTA_MAX_FILE_MB, "
                "'session': settings.QUOTA_MAX_SESSION_MB}))"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
    )
    assert json.loads(output) == {"file": 71, "session": 93}


def test_quota_accepts_60_and_90_mib_files_but_rejects_over_100_mib(monkeypatch) -> None:
    monkeypatch.setattr(settings, "QUOTA_MAX_FILE_MB", 100)
    monkeypatch.setattr(settings, "QUOTA_MAX_SESSION_MB", 100)

    for size_mb in (60, 90, 100):
        quota = _quota_snapshot(
            [],
            incoming_files=1,
            incoming_bytes=size_mb * MIB,
            incoming_file_sizes=[size_mb * MIB],
        )
        assert quota.allowed

    oversized = _quota_snapshot(
        [],
        incoming_files=1,
        incoming_bytes=101 * MIB,
        incoming_file_sizes=[101 * MIB],
    )
    assert not oversized.allowed
    assert any("파일 1개당 최대 100MB" in item for item in oversized.violations)


def test_session_quota_accepts_exactly_100_mib_and_rejects_more(monkeypatch) -> None:
    monkeypatch.setattr(settings, "QUOTA_MAX_FILE_MB", 100)
    monkeypatch.setattr(settings, "QUOTA_MAX_SESSION_MB", 100)
    current_docs = [{"file_size_bytes": 60 * MIB}]

    at_limit = _quota_snapshot(
        current_docs,
        incoming_files=1,
        incoming_bytes=40 * MIB,
        incoming_file_sizes=[40 * MIB],
    )
    assert at_limit.allowed

    over_limit = _quota_snapshot(
        current_docs,
        incoming_files=1,
        incoming_bytes=40 * MIB + 1,
        incoming_file_sizes=[40 * MIB + 1],
    )
    assert not over_limit.allowed
    assert any("세션당 총 업로드 용량은 최대 100MB" in item for item in over_limit.violations)


def test_openapi_advertises_runtime_quota_without_stale_50mb_examples() -> None:
    encoded = json.dumps(app.openapi(), ensure_ascii=False)

    assert "파일당 50MB" not in encoded
    assert '"max_file_mb": 50' not in encoded
    assert '"max_session_mb": 50' not in encoded
    assert settings.QUOTA_MAX_FILE_MB == 100
    assert settings.QUOTA_MAX_SESSION_MB == 100


def test_embedding_and_preprocessor_time_budgets_remain_bounded() -> None:
    assert settings.EXTERNAL_PREPROCESSOR_MAX_FILE_MB == 100
    assert settings.PDF_MAX_ESTIMATED_SECONDS == 300
    assert settings.EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES == 0
    assert settings.ROUTE_SOFT_CHUNK_LIMIT == 100_000
    assert settings.ROUTE_HARD_CHUNK_LIMIT == 200_000
    assert settings.WIKI_MAX_CHUNKS == 80
