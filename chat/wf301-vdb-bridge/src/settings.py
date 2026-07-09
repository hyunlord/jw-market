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
TEMP_DOCUMENT_DIR = os.environ.get("TEMP_DOCUMENT_DIR", "/nfs-root/temp-document")
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "5"))
SEARCH_CONTEXT_CHAR_LIMIT = int(os.environ.get("SEARCH_CONTEXT_CHAR_LIMIT", "8000"))

DB_HOST = os.environ.get("DB_HOST", "galera-mariadb-galera")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "llmops")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

TTL_DAYS = int(os.environ.get("TTL_DAYS", "7"))
QUOTA_MAX_FILES = int(os.environ.get("QUOTA_MAX_FILES", "10"))
QUOTA_MAX_PER_REQUEST = int(os.environ.get("QUOTA_MAX_PER_REQUEST", "10"))
QUOTA_MAX_FILE_MB = int(os.environ.get("QUOTA_MAX_FILE_MB", "50"))
QUOTA_MAX_SESSION_MB = int(os.environ.get("QUOTA_MAX_SESSION_MB", "50"))
ROUTE_SOFT_CHUNK_LIMIT = int(os.environ.get("ROUTE_SOFT_CHUNK_LIMIT", "100000"))
ROUTE_HARD_CHUNK_LIMIT = int(os.environ.get("ROUTE_HARD_CHUNK_LIMIT", "200000"))
EXTERNAL_PREPROCESSOR_MAX_FILE_MB = int(os.environ.get("EXTERNAL_PREPROCESSOR_MAX_FILE_MB", "20"))
EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES = int(os.environ.get("EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", "250"))
EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES = int(os.environ.get("EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES", "120"))

PREPROCESSOR_ID = int(os.environ.get("PREPROCESSOR_ID", "64"))
EMBEDDING_SERVING_ID = int(os.environ.get("EMBEDDING_SERVING_ID", "25"))
EMBEDDING_SERVING_REV_ID = int(os.environ.get("EMBEDDING_SERVING_REV_ID", "31"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "64"))
DEFAULT_USER_ID = int(os.environ.get("DEFAULT_USER_ID", "7"))

JS_COMPLETE = "JS0003"
VDB_DATA_TYPE_DOCUMENT = "VT0002"

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
