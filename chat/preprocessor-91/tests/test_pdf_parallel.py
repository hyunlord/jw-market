from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "src" / "preprocessor.py"
SPEC = importlib.util.spec_from_file_location("p91_preprocessor", MODULE_PATH)
assert SPEC and SPEC.loader
preprocessor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preprocessor
SPEC.loader.exec_module(preprocessor)


def test_effective_cpu_count_uses_cgroup_v2_quota(tmp_path: Path) -> None:
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("400000 100000\n", encoding="ascii")

    assert preprocessor._effective_cpu_count(cpu_max_path=cpu_max) == 4


def test_page_worker_count_honors_env_and_cpu_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREPROC_PAGE_WORKERS", "8")

    assert preprocessor._page_worker_count(cpu_count=4) == 4


def test_worker_process_limits_inner_math_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMP_NUM_THREADS", "16")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "16")

    preprocessor._set_page_worker_thread_limits(thread_count=1)

    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        assert preprocessor.os.environ[name] == "1"


def test_ocr_candidate_passes_configured_english_and_korean_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []

    class FakePage:
        def get_text(self, mode: str) -> str:
            assert mode == "text"
            return ""

    class FakeDocument:
        def __getitem__(self, page: int) -> FakePage:
            assert page == 0
            return FakePage()

    class FakePymupdf4llm:
        @staticmethod
        def to_markdown(document: object, **kwargs: object) -> list[dict[str, str]]:
            observed.append(kwargs)
            return [{"text": "recognized"}]

    monkeypatch.setattr(preprocessor, "PDF_OCR_LANGUAGES", "eng+kor", raising=False)

    items, used_ocr = preprocessor._page_markdown_items(
        FakePymupdf4llm,
        FakeDocument(),
        0,
    )

    assert used_ocr is True
    assert items == [{"text": "recognized"}]
    assert observed[0]["ocr_language"] == "eng+kor"


def test_ordered_page_results_restore_source_order() -> None:
    results = [
        preprocessor.PdfPageResult(2, "page-3", False, 0.3),
        preprocessor.PdfPageResult(0, "page-1", False, 0.1),
        preprocessor.PdfPageResult(1, "page-2", True, 0.2),
    ]

    ordered = preprocessor._ordered_page_results(results, expected_pages=3)

    assert [item.page_index for item in ordered] == [0, 1, 2]
    assert [item.markdown for item in ordered] == ["page-1", "page-2", "page-3"]


def test_page_failure_is_fail_closed() -> None:
    results = [
        preprocessor.PdfPageResult(0, "page-1", False, 0.1),
        preprocessor.PdfPageResult(1, "", False, 0.2, error="parser exploded"),
    ]

    with pytest.raises(preprocessor.PdfPageExtractionError, match="page 2"):
        preprocessor._ordered_page_results(results, expected_pages=2)


def test_large_pdf_threshold_is_page_or_size_based() -> None:
    assert preprocessor._is_large_pdf(page_count=50, file_size=1)
    assert preprocessor._is_large_pdf(page_count=1, file_size=5 * 1024 * 1024)
    assert not preprocessor._is_large_pdf(page_count=49, file_size=(5 * 1024 * 1024) - 1)


def test_large_pdf_gate_serializes_independent_callers(tmp_path: Path) -> None:
    lock_path = tmp_path / "large-pdf.lock"
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()

    def first() -> None:
        with preprocessor.LargePdfGate(lock_path):
            first_acquired.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        assert first_acquired.wait(timeout=2)
        with preprocessor.LargePdfGate(lock_path):
            second_acquired.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()

    assert first_acquired.wait(timeout=2)
    time.sleep(0.1)
    assert not second_acquired.is_set()
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_acquired.is_set()


def test_parallel_extractor_uses_spawn_pool_and_reorders_results() -> None:
    observed: dict[str, object] = {}

    class FakeExecutor:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def __enter__(self) -> "FakeExecutor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def map(self, fn: object, pages: range, chunksize: int = 1):
            observed["pages"] = list(pages)
            observed["chunksize"] = chunksize
            return [
                preprocessor.PdfPageResult(1, "page-2", False, 0.2),
                preprocessor.PdfPageResult(0, "page-1", False, 0.1),
            ]

    results = preprocessor._extract_pages_parallel(
        "/tmp/document.pdf",
        page_count=2,
        worker_count=2,
        executor_factory=FakeExecutor,
    )

    assert observed["max_workers"] == 2
    assert observed["mp_context"].get_start_method() == "spawn"
    assert observed["pages"] == [0, 1]
    assert [item.page_index for item in results] == [0, 1]


def test_deployment_contract_reserves_parallel_resources() -> None:
    manifest = (Path(__file__).parents[1] / "deploy" / "preprocessor-91-patch.yaml").read_text(
        encoding="utf-8"
    )
    source = (Path(__file__).parents[1] / "src" / "preprocessor.py").read_bytes()
    source_sha256 = hashlib.sha256(source).hexdigest()

    assert 'name: PREPROC_PAGE_WORKERS\n              value: "8"' in manifest
    assert "name: PREPROC_LARGE_PDF_MIN_PAGES" in manifest
    assert "name: PREPROC_LARGE_PDF_MIN_BYTES" in manifest
    assert 'name: PREPROC_PAGE_WORKER_THREADS\n              value: "1"' in manifest
    assert 'name: PDF_OCR_LANGUAGES\n              value: "eng+kor"' in manifest
    assert 'value: "50"' in manifest
    assert 'value: "5242880"' in manifest
    assert 'cpu: "8"' in manifest
    assert "memory: 16Gi" in manifest
    assert f"jw-market/source-sha256: {source_sha256}" in manifest
