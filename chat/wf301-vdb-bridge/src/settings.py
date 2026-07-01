"""Runtime settings for the wf301 VDB bridge."""

from __future__ import annotations

import os


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
EMBEDDING_BASE = os.environ.get(
    "EMBEDDING_BASE",
    f"http://llmops-gateway-api-service:8080/rep/serving/{os.environ.get('EMBEDDING_SERVING_ID', '25')}",
)
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "5"))
SEARCH_CONTEXT_CHAR_LIMIT = int(os.environ.get("SEARCH_CONTEXT_CHAR_LIMIT", "8000"))

DB_HOST = os.environ.get("DB_HOST", "galera-mariadb-galera")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "llmops")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

TTL_DAYS = int(os.environ.get("TTL_DAYS", "7"))
QUOTA_MAX_FILES = int(os.environ.get("QUOTA_MAX_FILES", "20"))
QUOTA_MAX_PER_REQUEST = int(os.environ.get("QUOTA_MAX_PER_REQUEST", "5"))
QUOTA_MAX_FILE_MB = int(os.environ.get("QUOTA_MAX_FILE_MB", "50"))
QUOTA_MAX_SESSION_MB = int(os.environ.get("QUOTA_MAX_SESSION_MB", "200"))

PREPROCESSOR_ID = int(os.environ.get("PREPROCESSOR_ID", "64"))
EMBEDDING_SERVING_ID = int(os.environ.get("EMBEDDING_SERVING_ID", "25"))
EMBEDDING_SERVING_REV_ID = int(os.environ.get("EMBEDDING_SERVING_REV_ID", "31"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "64"))
DEFAULT_USER_ID = int(os.environ.get("DEFAULT_USER_ID", "7"))

JS_COMPLETE = "JS0003"
VDB_DATA_TYPE_DOCUMENT = "VT0002"
