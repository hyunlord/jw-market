from pathlib import Path

import pytest

from pipeline.etl.io import iqvia_loader
from pipeline.etl.io.iqvia_loader import canonical_nsa_files, long_format_period_record


def test_connect_uses_runtime_mariadb_env_without_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(iqvia_loader, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("MARIADB_HOST", "mariadb.internal")
    monkeypatch.setenv("MARIADB_PORT", "3306")
    monkeypatch.setenv("MARIADB_USER", "writer")
    monkeypatch.setenv("MARIADB_PASSWORD", "secret")
    monkeypatch.delenv("HOST_PORT", raising=False)
    monkeypatch.setattr(
        iqvia_loader.pymysql,
        "connect",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    iqvia_loader.connect("jw_mart_rehearsal_r1")

    assert captured == {
        "host": "mariadb.internal",
        "port": 3306,
        "user": "writer",
        "password": "secret",
        "database": "jw_mart_rehearsal_r1",
        "charset": "utf8mb4",
        "autocommit": False,
    }


def test_canonical_nsa_files_keeps_workbook_extracts() -> None:
    files = [
        Path("data/IQVIA/NSA/~$KOR_NSA_Jun-25-2026.xlsx"),
        Path("data/IQVIA/NSA/KOR_NSA_Jun-25-2026.xlsx"),
        Path("data/IQVIA/NSA/legacy.csv"),
        Path("data/IQVIA/NSA/readme.txt"),
    ]

    assert canonical_nsa_files(files) == [
        Path("data/IQVIA/NSA/KOR_NSA_Jun-25-2026.xlsx"),
        Path("data/IQVIA/NSA/legacy.csv"),
    ]


def test_long_format_period_record_parses_data_period_rows() -> None:
    raw = {
        "DATA PERIOD": "2021-06-01 00:00:00",
        "AUDIT CODE": "KCPA",
        "AUDIT DESC": "Korea Direct Clinic Pharmaceutical Audit",
        "MFR CODE": "A+K",
        "MFR NAME": "AUSKOREA",
        "PRODUCT NAME": "AUSTAREN F",
        "PACK DESC": "A.IM 90MG 2ML",
        "Values LC": 7152613,
        "Units": 7537,
        "Counting Units": 15074,
        "Dosage Units": 7537,
        "Price": 949,
    }

    record = long_format_period_record(Path("KOR_NSA_Jun-25-2026.xlsx"), "NSA", 2, raw, list(raw))

    assert record is not None
    assert record["period_yyyy"] == 2021
    assert record["period_quarter"] == 2
    assert record["period_label"] == "2021Q2"
    assert record["audit_code"] == "KCPA"
