"""Runtime settings for the wf301 VDB bridge."""

from __future__ import annotations

import os


EXTERNAL_ROOT_PATH = os.environ.get("EXTERNAL_ROOT_PATH", "/api/gateway/code_serving/235")
OPENAPI_VERSION = os.environ.get("OPENAPI_VERSION", "upload-endpoint-68defd9-rootpath")
TARGET_VDB_ID = int(os.environ.get("TARGET_VDB_ID", "139"))
TARGET_VDB_COLLECTION = os.environ.get(
    "TARGET_VDB_COLLECTION", "Z87cf9950c34b41b48564482b4112613d"
)
COMMIT_ENABLED = os.environ.get("COMMIT_ENABLED", "false").lower() == "true"
ALLOWED_WORKFLOW_IDS = {
    int(x) for x in os.environ.get("ALLOWED_WORKFLOW_IDS", "301").split(",") if x.strip()
}

WEAVIATE_BASE = os.environ.get("WEAVIATE_BASE", "http://llmops-weaviate-service:8080")
HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "15"))
PREPROCESSOR_TIMEOUT_S = float(os.environ.get("PREPROCESSOR_TIMEOUT_S", "45"))
EMBEDDING_TIMEOUT_S = float(
    os.environ.get("EMBEDDING_TIMEOUT_S", str(max(HTTP_TIMEOUT_S, 60.0)))
)
EMBEDDING_BASE = os.environ.get(
    "EMBEDDING_BASE",
    f"http://llmops-gateway-api-service:8080/rep/serving/{os.environ.get('EMBEDDING_SERVING_ID', '25')}",
)
TEMP_VDB_INDEX_API_BASE = os.environ.get(
    "TEMP_VDB_INDEX_API_BASE", "http://llmops-temp-vdb-index-api-service:8080"
)
PREPROCESSOR_API_BASE = os.environ.get(
    "PREPROCESSOR_API_BASE", "http://llmops-preprocess-api-service:8080"
)
# fail-closed: preprocess-api(facade)는 downstream worker가 5xx/OOM으로 죽어도 예외를 삼켜
# HTTP 200 + {"code":1,"errMsg":...,"data":null} 응답을 돌려준다. 성공 envelope는 code==0.
# facade는 GenOS 공용이라 수정하지 않고, 235가 code를 검사해 조용한 실패를 명시 실패로 바꾼다.
PREPROCESSOR_ENVELOPE_SUCCESS_CODE = int(
    os.environ.get("PREPROCESSOR_ENVELOPE_SUCCESS_CODE", "0")
)
# blocked_uploads route는 스키마 Literal("preprocess_failed", blocked_oversized와 동형)이라
# chat ⑤ 3-상태에서 "확인 불가"로 매핑되며, 사유(reason)는 facade errMsg를 그대로 전달한다.
PDF_VLM_BASE = os.environ.get(
    "PDF_VLM_BASE", "http://llmops-gateway-api-service:8080/rep/serving/163"
)
PDF_VLM_MODEL = os.environ.get("PDF_VLM_MODEL", "genos/163/gemini-3.1-flash-lite")
PDF_VLM_MODEL_HEADER = os.environ.get("PDF_VLM_MODEL_HEADER", "gemini-3.1-flash-lite")
PDF_VLM_RENDER_DPI = int(os.environ.get("PDF_VLM_RENDER_DPI", "150"))
PDF_VLM_MAX_PAGES_PER_DOCUMENT = int(os.environ.get("PDF_VLM_MAX_PAGES_PER_DOCUMENT", "25"))
PDF_VLM_TIMEOUT_S = float(os.environ.get("PDF_VLM_TIMEOUT_S", "45"))
PDF_VLM_RETRIES = int(os.environ.get("PDF_VLM_RETRIES", "1"))
PDF_VLM_RETRY_BACKOFF_S = float(os.environ.get("PDF_VLM_RETRY_BACKOFF_S", "0.5"))
PDF_VLM_MAX_IMAGE_BYTES = int(os.environ.get("PDF_VLM_MAX_IMAGE_BYTES", str(7 * 1024 * 1024)))
PDF_VLM_IMAGE_COVERAGE_MIN = float(os.environ.get("PDF_VLM_IMAGE_COVERAGE_MIN", "0.28"))
PDF_VLM_NATIVE_CHAR_MAX = int(os.environ.get("PDF_VLM_NATIVE_CHAR_MAX", "1200"))
TEMP_DOCUMENT_DIR = os.environ.get("TEMP_DOCUMENT_DIR", "/nfs-root/temp-document")
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "5"))
SEARCH_CONTEXT_CHAR_LIMIT = int(os.environ.get("SEARCH_CONTEXT_CHAR_LIMIT", "8000"))

# 외부 PDF 전처리기가 텍스트 없는 페이지에 남기는 자리표시 청크 텍스트.
# 이 청크는 근거(evidence)가 아니므로 검색 컨텍스트에서 제외하고 상태 메타데이터로만 노출한다.
PDF_EMPTY_PAGE_MARKER = os.environ.get("PDF_EMPTY_PAGE_MARKER", "[empty_page]")
# 미보유 3-상태 연계용 상태 라벨: 페이지에 네이티브 텍스트가 없고 시각 채널 처리도 없던 경우.
PDF_EMPTY_PAGE_STATUS_NOT_PROCESSED = os.environ.get(
    "PDF_EMPTY_PAGE_STATUS_NOT_PROCESSED", "visual_content_not_processed"
)
# 같은 페이지를 VLM 시각 채널이 별도로 처리한 경우(자리표시 청크만 근거 제외).
PDF_EMPTY_PAGE_STATUS_PROCESSED = os.environ.get(
    "PDF_EMPTY_PAGE_STATUS_PROCESSED", "visual_content_processed"
)

DB_HOST = os.environ.get("DB_HOST", "galera-mariadb-galera")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "llmops")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

TTL_DAYS = int(os.environ.get("TTL_DAYS", "7"))
QUOTA_MAX_FILES = int(os.environ.get("QUOTA_MAX_FILES", "10"))
QUOTA_MAX_PER_REQUEST = int(os.environ.get("QUOTA_MAX_PER_REQUEST", "10"))
QUOTA_MAX_FILE_MB = int(os.environ.get("QUOTA_MAX_FILE_MB", "100"))
QUOTA_MAX_SESSION_MB = int(os.environ.get("QUOTA_MAX_SESSION_MB", "100"))
ROUTE_SOFT_CHUNK_LIMIT = int(os.environ.get("ROUTE_SOFT_CHUNK_LIMIT", "100000"))
ROUTE_HARD_CHUNK_LIMIT = int(os.environ.get("ROUTE_HARD_CHUNK_LIMIT", "200000"))
EXTERNAL_PREPROCESSOR_MAX_FILE_MB = int(os.environ.get("EXTERNAL_PREPROCESSOR_MAX_FILE_MB", "100"))
# Optional absolute emergency cap. PDF admission normally follows the measured time budget below.
EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES = int(os.environ.get("EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", "0"))
EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES = int(os.environ.get("EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES", "120"))
PDF_TEXT_LAYER_MIN_CHARS = max(
    int(
        os.environ.get(
            "PDF_TEXT_LAYER_MIN_CHARS",
            os.environ.get("PDF_OCR_TEXT_MIN_NONSPACE", "20"),
        )
    ),
    0,
)
PDF_TEXT_PAGE_SECONDS = max(float(os.environ.get("PDF_TEXT_PAGE_SECONDS", "0.38")), 0.0)
PDF_OCR_PAGE_SECONDS = max(float(os.environ.get("PDF_OCR_PAGE_SECONDS", "3.91")), 0.0)
PDF_MAX_ESTIMATED_SECONDS = max(float(os.environ.get("PDF_MAX_ESTIMATED_SECONDS", "300")), 0.0)
PDF_PROGRESSIVE_PREVIEW_PAGES = max(
    int(os.environ.get("PDF_PROGRESSIVE_PREVIEW_PAGES", "20")),
    0,
)

# 로컬 XLSX 전처리(청킹+임베딩+139 복사)의 fail-closed 타임아웃 게이트.
# XLSX_EMBED_CHUNKS_PER_SEC 근거: 2026-07-11 실측 — crosstab.xlsx 3,166청크가 약 158초
# (청킹 1.4초 + 임베딩/복사 나머지) ≈ 20 chunks/s. 임베딩 serving 처리량이 바뀌면 env로 조정.
XLSX_EMBED_CHUNKS_PER_SEC = float(os.environ.get("XLSX_EMBED_CHUNKS_PER_SEC", "20"))
# XLSX_UPLOAD_TIME_BUDGET_S 근거: chat→235 게이트웨이/클라이언트 타임아웃 180초에서
# 업로드 저장·원장 기록·응답 오버헤드 여유 30초를 뺀 값. 게이트웨이 타임아웃이 바뀌면 env로 조정.
XLSX_UPLOAD_TIME_BUDGET_S = float(os.environ.get("XLSX_UPLOAD_TIME_BUDGET_S", "150"))

PREPROCESSOR_ID = int(os.environ.get("PREPROCESSOR_ID", "64"))
EMBEDDING_SERVING_ID = int(os.environ.get("EMBEDDING_SERVING_ID", "25"))
EMBEDDING_SERVING_REV_ID = int(os.environ.get("EMBEDDING_SERVING_REV_ID", "31"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "64"))
DEFAULT_USER_ID = int(os.environ.get("DEFAULT_USER_ID", "7"))

JS_COMPLETE = "JS0003"
VDB_DATA_TYPE_DOCUMENT = "VT0002"

FILE_SQL_ENABLED = os.environ.get("FILE_SQL_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

WIKI_ENABLED = os.environ.get("WIKI_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
WIKI_AUTO_CREATE_SCHEMA = os.environ.get("WIKI_AUTO_CREATE_SCHEMA", "false").lower() in {"1", "true", "yes", "on"}
WIKI_SERVING_BASE = os.environ.get(
    "WIKI_SERVING_BASE",
    "http://llmops-gateway-api-service:8080/rep/serving/190",
).rstrip("/")
WIKI_SERVING_MODEL = os.environ.get("WIKI_SERVING_MODEL", "genos/190/gemini-3-flash-preview")
WIKI_MIN_DOCUMENTS = int(os.environ.get("WIKI_MIN_DOCUMENTS", "2"))
WIKI_MAX_CHUNKS = int(os.environ.get("WIKI_MAX_CHUNKS", "80"))
WIKI_CONTEXT_CHAR_LIMIT = int(os.environ.get("WIKI_CONTEXT_CHAR_LIMIT", "8000"))
WIKI_COMPILE_TIMEOUT_S = float(os.environ.get("WIKI_COMPILE_TIMEOUT_S", "60"))
WIKI_LOCK_TIMEOUT_S = int(os.environ.get("WIKI_LOCK_TIMEOUT_S", "1"))
