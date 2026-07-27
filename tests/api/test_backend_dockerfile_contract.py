from __future__ import annotations

from pathlib import Path


def test_backend_image_includes_api_import_dependencies() -> None:
    """Keep API transitive imports available in the slim backend image."""
    dockerfile = Path("api/Dockerfile").read_text()

    assert "COPY pipeline/contracts /app/pipeline/contracts" in dockerfile
    assert "COPY pipeline/domain /app/pipeline/domain" in dockerfile
    assert (
        'COPY ["data/JW 주요 약품 수동 매핑", '
        '"/app/data/JW 주요 약품 수동 매핑"]'
    ) in dockerfile
    assert "COPY pipeline/scripts/api /app/pipeline/scripts/api" in dockerfile
    assert "COPY pipeline/scripts/analysis /app/pipeline/scripts/analysis" in dockerfile
    assert "COPY pipeline/scripts/deploy /app/pipeline/scripts/deploy" in dockerfile


def test_backend_requirements_include_import_time_etl_dependencies() -> None:
    requirements = Path("pipeline/scripts/api/requirements.txt").read_text()

    assert "openpyxl>=3.1" in requirements
    assert "pyarrow==24.0.0" in requirements
    assert "duckdb==1.5.4" in requirements
