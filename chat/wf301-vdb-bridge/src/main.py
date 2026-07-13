"""wf301 VDB registration bridge.

The bridge copies already-preprocessed Temp VDB chunks into registered VDB 139
and creates the GenOS document/document_upsert ledger rows required for portal
list/delete behavior. The `/dry-run` endpoint is read-only; `/commit` is gated
by COMMIT_ENABLED and DB credentials supplied through Kubernetes secrets.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile

from . import delete_ops, ledger, pdf_vlm, session_wiki, settings, upload_adapter, weaviate_ops
from .file_sql import (
    FileSqlNotFoundError,
    FileSqlRejectedError,
    describe_schema_for_llm,
    drop_logical_table,
    provision_session_table,
    run_scoped_query,
)
from .logging_utils import safe_log
from .upload_ownership import TempDocumentNotFoundError, UploadOwnershipRegistry
from .models import (
    BlockedUpload,
    BridgeRequest,
    CommitDocumentResult,
    CommitResponse,
    DeleteDocumentRequest,
    DeleteDocumentResponse,
    DocumentPlan,
    DocumentsResponse,
    EmptyPageSource,
    FileSource,
    FileSqlColumn,
    FileSqlQueryRequest,
    FileSqlQueryResponse,
    FileSqlSchemaRequest,
    FileSqlSchemaResponse,
    FileSqlSource,
    DryRunResponse,
    PlannedDocumentRow,
    PlannedUpsertRow,
    QuotaCheckResponse,
    QuotaLimits,
    QuotaSnapshot,
    PublicCommitResponse,
    PublicDocumentsResponse,
    PublicSearchResponse,
    PublicUploadResponse,
    SearchRequest,
    SearchResponse,
    SessionDocument,
    SessionRequest,
    SqlTableMetadata,
    TempDocument,
    UploadedTempDocument,
    UploadResponse,
)
from .xlsx_preprocessor import (
    SheetSkip,
    XlsxPreprocessError,
    extract_xlsx_chunks,
    iter_xlsx_chunks,
    should_stream_xlsx_chunks,
)
from .docx_preprocessor import DocxPreprocessError, extract_docx_chunks
from .xlsx_sql_route import (
    WorkbookSqlDecision,
    inspect_xlsx_for_sql,
    load_sql_sheet,
    logical_names_for_profiles,
    workbook_storage_route,
)

WORKFLOW_ID_EXAMPLE = 301
SESSION_KEY_EXAMPLE = "puc-004928"
SESSION_KEY_SCHEMA = {"maxLength": 36, "pattern": "^[A-Za-z0-9_-]{1,36}$"}
TARGET_VDB_EXAMPLE = settings.TARGET_VDB_ID
# .xlsm은 매크로(vbaProject)를 무시하고 데이터 시트만 .xlsx와 같은 로컬 전처리 경로로 처리한다.
LOCAL_XLSX_SUFFIXES = (".xlsx", ".xlsm")
_UPLOAD_OWNERSHIP = UploadOwnershipRegistry(Path(settings.TEMP_DOCUMENT_DIR))


def _xlsx_timeout_gate(chunk_count: int) -> tuple[str, str] | None:
    """Fail-closed gate: 임베딩이 업로드 시간 예산을 넘길 XLSX는 조용한 부분 색인 대신 명시 차단."""
    limit = int(settings.XLSX_EMBED_CHUNKS_PER_SEC * settings.XLSX_UPLOAD_TIME_BUDGET_S)
    if chunk_count <= limit:
        return None
    estimated_s = chunk_count / settings.XLSX_EMBED_CHUNKS_PER_SEC
    reason = (
        f"chunk_count={chunk_count} exceeds timeout-safe xlsx commit limit {limit} "
        f"(estimated embedding {estimated_s:.0f}s > budget {settings.XLSX_UPLOAD_TIME_BUDGET_S:.0f}s); "
        "파일이 커서 제한 시간 안에 색인을 마칠 수 없어 등록을 차단했습니다. "
        "시트를 나누거나 필요한 데이터만 추려 다시 업로드해 주세요."
    )
    return ("blocked_oversized", reason)


def _chunk_route(chunk_count: int) -> tuple[str, str]:
    if chunk_count > settings.ROUTE_HARD_CHUNK_LIMIT:
        return (
            "blocked_oversized",
            f"chunk_count={chunk_count} exceeds hard limit {settings.ROUTE_HARD_CHUNK_LIMIT}; "
            "VDB commit is blocked for this document, review Wiki/ETL routing.",
        )
    if chunk_count > settings.ROUTE_SOFT_CHUNK_LIMIT:
        return (
            "vdb_large",
            f"chunk_count={chunk_count} exceeds soft limit {settings.ROUTE_SOFT_CHUNK_LIMIT} "
            f"but is within hard limit {settings.ROUTE_HARD_CHUNK_LIMIT}; VDB commit is allowed with warning.",
        )
    return (
        "vdb",
        f"chunk_count={chunk_count} is within VDB soft limit {settings.ROUTE_SOFT_CHUNK_LIMIT}.",
    )


def _blocked_upload_models(
    blocks: list[upload_adapter.PreprocessorGateBlock],
) -> list[BlockedUpload]:
    return [
        BlockedUpload(
            file_name=block.file_name,
            route=block.route,
            route_reason=block.route_reason,
            file_size_bytes=block.file_size_bytes,
        )
        for block in blocks
    ]


def _preprocessor_gate_block_for_temp_doc(
    temp_doc: TempDocument,
) -> upload_adapter.PreprocessorGateBlock | None:
    if not temp_doc.file_path:
        return None
    document = upload_adapter.SavedTempDocument(
        temp_document_id=temp_doc.temp_document_id,
        file_name=temp_doc.file_name,
        file_path=temp_doc.file_path,
    )
    blocks = upload_adapter.blocked_saved_external_preprocessor_documents([document])
    return blocks[0] if blocks else None

API_DESCRIPTION = """
wf301 파일 업로드와 세션별 문서 검색을 연결하는 code-serving-235 브리지 API입니다.

이 서비스는 파일을 임시 VDB에 먼저 올린 뒤, 같은 `/upload` 요청 안에서 공용 VDB 139와 GenOS 문서 원장 등록까지 이어서 수행합니다. 표준 호출 흐름은 `POST /upload` -> `POST /search` -> `/documents/delete` 순서입니다. `/upload` 응답에 포함된 `commit` 결과가 성공이고 `file_only_ready=true`이면 `/search`와 `/documents`에서 바로 확인되는 등록 문서가 됩니다. `/commit` endpoint는 기존 클라이언트의 재시도와 하위호환을 위한 안전망으로 유지됩니다.

공통 제약:
- `workflow_id`는 wf301 파일 업로드 워크플로 전용 값인 `301`을 사용합니다.
- `vdb_id`는 등록 대상 공용 VDB인 `139`가 기본값이며, 다른 값은 거부됩니다.
- 세션 키는 `chat_id`가 있으면 `chat_id`, 없으면 `app_session_id`를 사용합니다. 같은 파일 묶음은 업로드, 커밋, 검색, 삭제까지 같은 세션 키를 계속 사용해야 합니다.
- 임시 VDB 계층은 세션 키를 36자 컬럼에 저장합니다. 37자 이상은 temp-vdb-index 계층에서 `09040008` 오류가 발생할 수 있습니다. 영문, 숫자, 하이픈, 언더스코어 조합과 36자 UUID 문자열을 권장합니다.
- 기본 쿼터는 세션당 10개, 요청당 10개, 파일당 50MB, 세션당 총 50MB입니다.
- 등록 문서 TTL은 7일입니다.
"""

HEALTH_DESCRIPTION = """
서비스가 요청을 받을 수 있는지와 런타임 제한값을 확인합니다.

내부적으로 애플리케이션 설정을 읽어 commit 모드 활성화 여부, DB 자격 설정 여부, 대상 VDB ID, TTL, 현재 쿼터 한도를 반환합니다. 외부 저장소에 쓰지 않는 읽기 전용 헬스 체크입니다.

언제 사용하나요:
- 배포 직후 code-serving-235가 정상 기동했는지 확인할 때
- `/upload` 또는 `/commit` 전에 현재 서비스가 dry-run 모드인지 commit 모드인지 확인할 때
- 프론트/게이트웨이에서 사용할 수 있는 경로와 쿼터 한도를 빠르게 표시할 때

응답 예시:
```json
{
  "status": "ok",
  "service": "wf301-vdb-bridge",
  "mode": "commit",
  "commit_enabled": true,
  "target_vdb_id": 139,
  "ttl_days": 7
}
```
"""

DRY_RUN_DESCRIPTION = """
임시 VDB에 이미 생성된 `temp_documents`를 공용 VDB 139에 등록하면 어떤 작업이 일어나는지 미리 계산합니다.

내부적으로 `workflow_id`와 `vdb_id`를 검증하고, 각 `temp_document_id`의 임시 청크를 조회한 뒤, `/commit`이 만들 `document`, `document_upsert`, VDB 139 객체 수와 idempotency key를 계획으로 반환합니다. DB insert, Weaviate copy, 문서 원장 변경은 수행하지 않습니다.

언제 사용하나요:
- 운영 쓰기 없이 파일명, 청크 수, 벡터 차원, 중복 방지 키를 점검할 때
- 장애 분석 중 “commit을 실행했다면 무엇을 썼을지”를 확인할 때
- 과거 방식처럼 `/upload`에서 받은 `temp_documents`를 별도 확정하기 전에 수동으로 검토할 때

호출 관계:
- `/upload` 응답의 `temp_documents` 배열을 그대로 전달하면 됩니다.
- 표준 `/upload`는 내부 commit까지 수행하므로 일반 클라이언트는 별도 `/dry-run`이 필요하지 않습니다.
- `/dry-run`은 검색 가능 상태를 만들지 않습니다. 수동/하위호환 흐름에서만 같은 payload로 `/commit`을 성공시켜야 검색할 수 있습니다.

요청 예시:
```json
{
  "workflow_id": 301,
  "vdb_id": 139,
  "app_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "temp_documents": [
    {"temp_document_id": 901, "file_name": "market.txt"}
  ]
}
```
"""

COMMIT_DESCRIPTION = """
`/upload`로 만들어진 임시 문서를 공용 VDB 139와 GenOS 문서 원장에 정식 등록합니다.

내부 단계:
1. `workflow_id=301`, `vdb_id=139`, 세션 키를 검증합니다.
2. 같은 세션의 기존 등록 문서와 신규 파일 크기를 기준으로 쿼터를 계산합니다.
3. 각 `temp_document_id`의 임시 청크를 읽고, 이미 같은 `source_doc_key`로 등록된 문서는 중복으로 보고 건너뜁니다.
4. 신규 문서는 GenOS `document`와 `document_upsert` 원장 row를 만들고, 청크를 공용 VDB 139 컬렉션으로 복사합니다.
5. 성공한 문서 수, 건너뛴 중복 수, rollback 참고 정보, 세션 문서 수를 반환합니다.

언제 사용하나요:
- 기존 클라이언트가 `/upload` 응답의 `temp_documents`를 별도 확정 등록할 때
- `/upload` 내부 commit 단계가 실패한 뒤 같은 임시 문서를 재시도할 때
- 운영자가 idempotency/dedup 안전망을 확인하며 수동으로 정식 등록할 때

중요:
- 표준 `/upload`는 내부에서 이 commit 로직을 자동 실행합니다. 정상 클라이언트는 `/upload` 한 번만 호출하면 됩니다.
- 이 endpoint는 하위호환/재시도용으로 유지됩니다. 이미 등록된 `source_doc_key`는 중복으로 보고 `skipped_duplicate` 처리됩니다.
- 같은 세션 키를 계속 사용해야 `/documents`, `/quota/check`, `/search`, `/documents/delete`가 같은 문서 묶음을 봅니다.
- `commit_enabled=false`이면 쓰기를 하지 않고 `commit stage is disabled` 오류를 반환합니다.
"""

UPLOAD_DESCRIPTION = """
파일을 세션 전용 임시 VDB에 업로드하고, 전처리 성공 후 같은 요청 안에서 공용 VDB 139 정식 등록까지 실행합니다.

내부 단계:
1. multipart form의 파일 목록과 `workflow_id`, 세션 키, `vdb_id`를 검증합니다.
2. workflow 301의 파일 업로드 플러그인 설정에서 허용 확장자, 전처리기, embedding serving, batch size, TTL을 읽습니다.
3. 현재 세션에 이미 commit된 문서 수와 업로드하려는 파일 크기로 쿼터를 계산합니다.
4. temp-vdb-index를 만들고 `temp_document` row와 로컬 임시 파일을 생성합니다.
5. 전처리기를 호출해 임시 VDB 청크를 생성합니다.
6. 기존 `/commit`과 같은 등록 로직으로 GenOS `document`/`document_upsert` 원장과 공용 VDB 139 객체를 생성합니다.
7. 성공 시 `temp_documents`와 함께 내부 commit 결과를 `commit` 필드로 반환합니다.

언제 사용하나요:
- 파일 업로드 한 번으로 wf301 채팅 검색 컨텍스트에 바로 사용할 문서를 등록할 때
- 업로드 직후 같은 세션에서 `/search`, `/documents`, `/documents/delete`를 이어서 사용할 때

중요:
- `/upload` 응답이 돌아왔을 때 `commit.errors`가 비어 있고 `commit.file_only_ready=true`이면 검색 가능한 상태입니다.
- 내부 commit이 quota/청크 없음 등으로 실패하면 `errors`와 `commit.errors`에 사유가 들어가며, 그 응답은 검색 가능 완료로 취급하면 안 됩니다.
- `/commit` endpoint는 하위호환과 재시도용으로 남아 있습니다. 같은 `temp_documents`로 다시 호출하면 기존 `source_doc_key` 중복은 `skipped_duplicate`로 처리됩니다.
- `commit_enabled=false`이면 임시 리소스를 만들기 전에 실패 응답을 반환합니다.
- 세션 키는 `chat_id`가 있으면 `chat_id`, 없으면 `app_session_id`입니다. 두 값을 섞어 쓰면 이후 목록/검색/삭제에서 다른 세션으로 인식될 수 있습니다.
- 세션 키는 36자 이하를 권장합니다. 37자 이상은 temp-vdb-index 계층에서 `09040008` 오류가 발생할 수 있습니다.

multipart 요청 예시:
```text
files=@market.txt
workflow_id=301
app_session_id=550e8400-e29b-41d4-a716-446655440000
vdb_id=139
```

응답 예시:
```json
{
  "mode": "upload",
  "target_vdb_id": 139,
  "workflow_id": 301,
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "temp_documents": [
    {"temp_document_id": 901, "file_name": "market.txt", "file_path": "/tmp/TEMP_DOCUMENT_901.txt"}
  ],
  "commit": {
    "mode": "commit",
    "committed_count": 1,
    "file_only_ready": true,
    "errors": []
  },
  "errors": []
}
```
"""

DOCUMENTS_DESCRIPTION = """
같은 세션 키로 `/upload` 내부 commit 또는 별도 `/commit`까지 완료된 활성 문서 목록을 조회합니다.

내부적으로 GenOS 문서 원장의 description JSON에서 `workflow_id`와 세션 키가 일치하는 문서를 찾고, TTL 만료 여부, 파일 크기, 청크 수, 원본 temp 문서 ID를 함께 반환합니다. 내부 commit 또는 별도 `/commit`이 실패한 임시 문서는 이 목록에 나오지 않습니다.

언제 사용하나요:
- 채팅 화면에서 현재 세션에 등록되어 검색 가능한 파일 목록을 보여줄 때
- `/documents/delete` 전에 삭제 대상 `document_id` 또는 `temp_document_id`를 찾을 때
- `/commit` 이후 실제 등록 여부를 확인할 때

응답 예시:
```json
{
  "target_vdb_id": 139,
  "workflow_id": 301,
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "documents": [
    {"document_id": 1234, "file_name": "market.txt", "temp_document_id": 901, "is_expired": false}
  ],
  "errors": []
}
```
"""

DELETE_DESCRIPTION = """
세션에 등록된 문서 하나를 비활성화하고, 연결된 VDB 139 객체를 삭제합니다.

내부적으로 요청 세션이 해당 문서의 description JSON과 일치하는지 확인한 뒤, `document_id` 또는 `temp_document_id`로 삭제 대상을 찾습니다. 권한이 맞으면 GenOS `document`/`document_upsert`를 비활성화하고 Weaviate 객체 삭제를 시도합니다. 응답에는 삭제된 객체 ID와 rollback 참고 정보가 포함됩니다.

언제 사용하나요:
- 사용자가 채팅 세션에서 업로드한 파일을 검색 대상에서 제거할 때
- `/documents` 목록에서 특정 파일을 선택해 삭제할 때

중요:
- 가능하면 `document_id` 또는 `temp_document_id` 중 하나만 보내세요. 둘 다 없으면 `document_id or temp_document_id is required` 오류가 반환됩니다.
- `/upload`만 되었고 `/commit`되지 않은 임시 파일은 등록 문서가 아니므로 이 API의 주 대상이 아닙니다.

요청 예시:
```json
{
  "workflow_id": 301,
  "vdb_id": 139,
  "app_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "document_id": 1234
}
```
"""

DELETE_DELETE_DESCRIPTION = """
`/documents/delete`의 DELETE 메서드 변형입니다.

요청 body와 응답 schema는 POST 변형과 같습니다. 클라이언트 또는 게이트웨이 정책상 삭제 동작을 HTTP DELETE로 표현해야 할 때 사용합니다. 실제 내부 동작은 POST `/documents/delete`와 동일하게 세션 검증, 삭제 대상 조회, GenOS 원장 비활성화, VDB 객체 삭제 순서로 진행됩니다.
"""

QUOTA_DESCRIPTION = """
현재 세션의 업로드 쿼터 상태를 조회합니다.

내부적으로 `/commit`까지 완료된 세션 문서를 기준으로 현재 파일 수와 byte 합계를 계산하고, 설정된 한도와 함께 반환합니다. `/upload` 직후 `/commit` 전에는 `current_files`가 아직 증가하지 않을 수 있으므로, 실제 검색 가능한 등록 문서 기준 쿼터는 `/commit` 후 이 API로 확인하세요.

언제 사용하나요:
- 업로드 버튼을 활성화하기 전에 세션 잔여 파일 수와 용량을 확인할 때
- 업로드 실패 원인이 파일 수/용량 한도인지 UI에 설명할 때
- `/commit` 후 실제 세션 등록량을 확인할 때

응답 예시:
```json
{
  "quota": {
    "limits": {"max_files": 10, "max_per_request": 10, "max_file_mb": 50, "max_session_mb": 50},
    "current_files": 1,
    "current_bytes": 1024,
    "allowed": true,
    "violations": []
  }
}
```
"""

SEARCH_DESCRIPTION = """
같은 세션에 commit된 문서만 대상으로 벡터 검색을 수행하고, wf301 채팅에 주입할 `file_context`와 출처 목록을 반환합니다.

내부 단계:
1. `workflow_id`, `vdb_id`, 세션 키를 검증합니다.
2. GenOS 문서 원장에서 같은 세션의 활성 문서 ID를 조회합니다.
3. 문서가 없으면 빈 `file_context`와 `result_count=0`을 반환합니다.
4. 질문을 embedding하고 VDB 139에서 해당 문서 ID들로 제한한 벡터 검색을 실행합니다.
5. 검색 청크를 문자 수 제한 안에서 합쳐 `file_context`를 만들고, 각 청크의 `document_id`, `file_name`, page/chunk 정보, distance를 `file_sources`로 반환합니다.

언제 사용하나요:
- `/commit`이 끝난 파일들을 기반으로 채팅 답변용 근거 컨텍스트를 만들 때
- 화면에서 “이 질문에 대해 업로드 파일 중 어떤 부분이 검색됐는지”를 확인할 때

중요:
- `/upload`만 된 파일은 검색되지 않습니다. `/commit` 성공 후 같은 세션 키로 호출해야 합니다.
- `limit`을 생략하면 서비스 기본 검색 개수를 사용합니다.

요청 예시:
```json
{
  "workflow_id": 301,
  "vdb_id": 139,
  "app_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "question": "리바로 2026년 1월 매출은?",
  "limit": 5
}
```
"""

app = FastAPI(
    title="wf301-vdb-bridge",
    version="api-0.1.0",
    description=API_DESCRIPTION,
    root_path="/api/gateway/code_serving/235",
)


def _guard(req: BridgeRequest) -> list[str]:
    errors: list[str] = []
    if req.workflow_id not in settings.ALLOWED_WORKFLOW_IDS:
        errors.append(f"workflow_id {req.workflow_id} not allowed")
    if req.vdb_id != settings.TARGET_VDB_ID:
        errors.append(f"vdb_id {req.vdb_id} != target {settings.TARGET_VDB_ID}")
    return errors


def _session_guard(req: SessionRequest) -> list[str]:
    errors: list[str] = []
    if req.workflow_id not in settings.ALLOWED_WORKFLOW_IDS:
        errors.append(f"workflow_id {req.workflow_id} not allowed")
    if req.vdb_id != settings.TARGET_VDB_ID:
        errors.append(f"vdb_id {req.vdb_id} != target {settings.TARGET_VDB_ID}")
    return errors


def _session_id(req: SessionRequest | BridgeRequest) -> str:
    return req.chat_id or req.app_session_id


def _audit_hash(value: str | int | None) -> str:
    return hashlib.sha256(str(value or "anonymous").encode("utf-8")).hexdigest()[:16]


def _request_id(request: Request | None) -> str:
    if request is not None:
        supplied = request.headers.get("x-request-id", "").strip()
        if supplied and len(supplied) <= 128:
            return supplied
    return uuid.uuid4().hex


def _owned_bridge_request(
    req: BridgeRequest,
    *,
    request_id: str,
) -> BridgeRequest:
    session_id = _session_id(req)
    temp_ids = [item.temp_document_id for item in req.temp_documents]
    try:
        owned = _UPLOAD_OWNERSHIP.resolve_many(session_id, req.workflow_id, temp_ids)
    except TempDocumentNotFoundError as exc:
        safe_log(
            "temp_ownership_decision",
            request_id=request_id,
            principal_hash=_audit_hash(req.user_id),
            session_hash=_UPLOAD_OWNERSHIP.session_hash(session_id),
            temp_document_ids=temp_ids,
            decision="deny",
            reason="not_owned_or_unavailable",
        )
        raise HTTPException(status_code=404, detail="temporary document not found") from exc
    metadata_matches = all(
        supplied.file_name == registered.file_name
        and (
            supplied.file_path is None
            or supplied.file_path == str(registered.file_path)
        )
        for supplied, registered in zip(req.temp_documents, owned, strict=True)
    )
    if not metadata_matches:
        safe_log(
            "temp_ownership_decision",
            request_id=request_id,
            principal_hash=_audit_hash(req.user_id),
            session_hash=_UPLOAD_OWNERSHIP.session_hash(session_id),
            temp_document_ids=temp_ids,
            decision="deny",
            reason="metadata_mismatch",
        )
        raise HTTPException(status_code=404, detail="temporary document not found")
    safe_log(
        "temp_ownership_decision",
        request_id=request_id,
        principal_hash=_audit_hash(req.user_id),
        session_hash=_UPLOAD_OWNERSHIP.session_hash(session_id),
        temp_document_ids=temp_ids,
        decision="allow",
        reason="session_ledger_match",
    )
    return req.model_copy(
        update={
            "temp_documents": [
                TempDocument(
                    temp_document_id=item.temp_document_id,
                    file_name=item.file_name,
                    file_path=str(item.file_path),
                )
                for item in owned
            ]
        }
    )


def _expires_at() -> str:
    expires = datetime.now(timezone.utc) + timedelta(days=settings.TTL_DAYS)
    return expires.isoformat()


def _quota_limits() -> QuotaLimits:
    return QuotaLimits(
        max_files=settings.QUOTA_MAX_FILES,
        max_per_request=settings.QUOTA_MAX_PER_REQUEST,
        max_file_mb=settings.QUOTA_MAX_FILE_MB,
        max_session_mb=settings.QUOTA_MAX_SESSION_MB,
    )


def _quota_snapshot(
    current_docs: list[dict[str, Any]],
    *,
    incoming_files: int = 0,
    incoming_bytes: int = 0,
    incoming_file_sizes: list[int] | None = None,
) -> QuotaSnapshot:
    limits = _quota_limits()
    current_bytes = sum(int(doc.get("file_size_bytes") or 0) for doc in current_docs)
    violations: list[str] = []
    notes: list[str] = []
    if incoming_files > limits.max_per_request:
        violations.append(f"한 번에 업로드할 수 있는 파일은 최대 {limits.max_per_request}개입니다.")
    if len(current_docs) + incoming_files > limits.max_files:
        violations.append(f"세션당 파일은 최대 {limits.max_files}개까지 업로드할 수 있습니다.")
    max_file_bytes = limits.max_file_mb * 1024 * 1024
    for size in incoming_file_sizes or []:
        if size > max_file_bytes:
            violations.append(f"파일 1개당 최대 {limits.max_file_mb}MB까지 업로드할 수 있습니다.")
    if incoming_file_sizes and any(size == 0 for size in incoming_file_sizes):
        notes.append("one or more incoming files had no file_size metadata")
    max_session_bytes = limits.max_session_mb * 1024 * 1024
    if current_bytes + incoming_bytes > max_session_bytes:
        violations.append(f"세션당 총 업로드 용량은 최대 {limits.max_session_mb}MB까지 허용됩니다.")
    return QuotaSnapshot(
        limits=limits,
        current_files=len(current_docs),
        current_bytes=current_bytes,
        incoming_files=incoming_files,
        incoming_bytes=incoming_bytes,
        allowed=not violations,
        violations=violations,
        notes=notes,
    )


def _description(
    req: BridgeRequest,
    *,
    source_doc_key: str,
    idempotency_key: str,
    temp_document_id: int,
    source_collection: str | None,
    expires_at: str,
    file_size_bytes: int,
    storage_route: str = "vdb",
    route_reason: str = "",
    sql_tables: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "workflow_id": req.workflow_id,
        "app_session_id": req.app_session_id,
        "chat_id": req.chat_id,
        "user_id": req.user_id,
        "temp_document_id": temp_document_id,
        "source_doc_key": source_doc_key,
        "idempotency_key": idempotency_key,
        "source_collection": source_collection,
        "expires_at": expires_at,
        "ttl_days": settings.TTL_DAYS,
        "file_size_bytes": file_size_bytes,
        "bridge": "wf301-vdb-bridge",
        "storage_route": storage_route,
        "route_reason": route_reason,
        "sql_tables": sql_tables or [],
    }


def _sql_decision_for_temp_doc(temp_doc: TempDocument) -> WorkbookSqlDecision | None:
    if not temp_doc.file_name.lower().endswith(LOCAL_XLSX_SUFFIXES) or not temp_doc.file_path:
        return None
    path = Path(temp_doc.file_path)
    if not path.is_file():
        return None
    decision = inspect_xlsx_for_sql(path)
    safe_log(
        "xlsx_storage_route",
        file_name=temp_doc.file_name,
        route=decision.route,
        route_reason=decision.reason,
        sheet_profiles=[profile.audit_dict() for profile in decision.profiles],
    )
    return decision


def _sql_table_metadata(
    temp_doc: TempDocument,
    decision: WorkbookSqlDecision,
) -> list[SqlTableMetadata]:
    return [
        SqlTableMetadata(
            logical_name=logical_name,
            sheet_name=profile.sheet_name,
            row_count=profile.row_count,
            column_count=profile.column_count,
        )
        for profile, logical_name in zip(
            decision.selected_sheets,
            logical_names_for_profiles(
                decision.selected_sheets,
                scope_prefix=f"doc_{temp_doc.temp_document_id}",
            ),
            strict=True,
        )
    ]


def _no_native_text_page_notes(enrichment: pdf_vlm.VisualEnrichment) -> list[str]:
    """네이티브 텍스트가 없는 페이지의 시각 처리 상태를 명시 고지 노트로 만든다.

    외부 전처리기는 이런 페이지를 색인에서 조용히 누락시킨다. 근거로 오용될 청크는 없지만
    사용자/chat이 '해당 페이지 정보는 없음' 대신 '처리되지 않았음'을 구분하도록 상태를 남긴다.
    VLM이 정상 처리한 페이지(selected에서 failed 제외)는 not_processed로 오분류하지 않는다.
    """
    if not enrichment.no_native_text_pages:
        return []
    visual_ok = set(enrichment.selected_pages) - set(enrichment.failed_pages)
    not_processed = [page for page in enrichment.no_native_text_pages if page not in visual_ok]
    processed = [page for page in enrichment.no_native_text_pages if page in visual_ok]
    notes: list[str] = []
    if not_processed:
        notes.append(
            f"no_native_text_pages={not_processed} "
            f"status={settings.PDF_EMPTY_PAGE_STATUS_NOT_PROCESSED}; "
            "텍스트가 없는 페이지는 색인되지 않았으며 시각 콘텐츠도 처리되지 않았습니다."
        )
    if processed:
        notes.append(
            f"no_native_text_pages={processed} "
            f"status={settings.PDF_EMPTY_PAGE_STATUS_PROCESSED}; "
            "텍스트가 없는 페이지지만 VLM 시각 채널로 별도 처리되었습니다."
        )
    return notes


def _annotate_empty_page_chunks(
    chunks: list[weaviate_ops.Chunk],
    *,
    visual_pages: set[Any],
) -> list[str]:
    """빈 페이지 자리표시 청크에 상태 provenance를 기록하고 사용자 고지 노트를 돌려준다.

    청크 수/본문 텍스트는 그대로 두고 summary provenance만 갱신하므로 라우팅·카운트에는
    영향이 없다. VLM 시각 채널이 처리한 페이지(visual_pages)는 not_processed로 오분류하지
    않는다.
    """
    not_processed_pages: list[Any] = []
    processed_pages: list[Any] = []
    for chunk in chunks:
        provenance = _hit_provenance(chunk)
        if str(provenance.get("source_channel") or "") == pdf_vlm.SOURCE_CHANNEL:
            continue
        if not _is_empty_page_marker_text(str(chunk.get("text") or "")):
            continue
        page = chunk.get("i_page")
        visual_processed = page in visual_pages
        provenance.update(
            {
                "empty_page": True,
                "visual_processed": visual_processed,
                "empty_page_status": settings.PDF_EMPTY_PAGE_STATUS_PROCESSED
                if visual_processed
                else settings.PDF_EMPTY_PAGE_STATUS_NOT_PROCESSED,
            }
        )
        chunk["summary"] = json.dumps(provenance, ensure_ascii=False, separators=(",", ":"))
        (processed_pages if visual_processed else not_processed_pages).append(page)
    notes: list[str] = []
    if not_processed_pages:
        notes.append(
            f"empty_pages={sorted(not_processed_pages, key=str)} "
            f"status={settings.PDF_EMPTY_PAGE_STATUS_NOT_PROCESSED}; "
            "텍스트가 없는 페이지의 자리표시 청크는 검색 근거에서 제외됩니다."
        )
    if processed_pages:
        notes.append(
            f"empty_pages={sorted(processed_pages, key=str)} "
            f"status={settings.PDF_EMPTY_PAGE_STATUS_PROCESSED}; "
            "해당 페이지는 VLM 시각 채널로 별도 처리되었습니다."
        )
    return notes


def _load_temp_chunks(
    client: httpx.Client, temp_doc: TempDocument, *, enrich_visual: bool = False
) -> tuple[str | None, list[weaviate_ops.Chunk], list[str]]:
    notes: list[str] = []
    local_xlsx = _load_local_xlsx_chunks(client, temp_doc)
    if local_xlsx is not None:
        return local_xlsx
    local_docx = _load_local_docx_chunks(client, temp_doc)
    if local_docx is not None:
        return local_docx
    classes = weaviate_ops.schema_classes(client)
    candidates = weaviate_ops.candidate_temp_classes(classes)
    collection = weaviate_ops.resolve_temp_collection(client, candidates, temp_doc.temp_document_id)
    if not collection:
        return None, [], ["temp collection not resolved (no candidate matched)"]
    try:
        chunks = weaviate_ops.read_temp_chunks(client, collection, temp_doc.temp_document_id)
    except httpx.HTTPError as exc:
        return collection, [], [f"chunk read failed: {exc}"]
    if not chunks:
        notes.append("no chunks found for temp document")
    if chunks and weaviate_ops.first_vector_dim(chunks) is None:
        notes.append("vector not present in chunk read")
    if enrich_visual and chunks and temp_doc.file_path and temp_doc.file_name.lower().endswith(".pdf"):
        path = Path(temp_doc.file_path)
        if path.is_file():
            try:
                enrichment = pdf_vlm.enrich_pdf_chunks(
                    client,
                    path=path,
                    temp_document_id=temp_doc.temp_document_id,
                    file_name=temp_doc.file_name,
                    native_chunks=chunks,
                    embed_texts=lambda texts: weaviate_ops.embed_texts(client, texts),
                )
                chunks = enrichment.chunks
                notes.extend(enrichment.notes)
                notes.append(
                    f"pdf_visual_status={enrichment.status} "
                    f"suspect_pages={enrichment.suspect_pages} selected_pages={enrichment.selected_pages} "
                    f"tokens={enrichment.usage.total_tokens} elapsed_s={enrichment.elapsed_s:.3f}"
                )
                notes.extend(_no_native_text_page_notes(enrichment))
            except Exception as exc:
                notes.append(f"pdf_visual_status=partial_visual visual_failed error={type(exc).__name__}: {exc}")
    if chunks and temp_doc.file_name.lower().endswith(".pdf"):
        visual_pages = {
            chunk.get("i_page")
            for chunk in chunks
            if str(_hit_provenance(chunk).get("source_channel") or "") == pdf_vlm.SOURCE_CHANNEL
        }
        notes.extend(_annotate_empty_page_chunks(chunks, visual_pages=visual_pages))
    return collection, chunks, notes


def _load_local_xlsx_chunks(
    client: httpx.Client, temp_doc: TempDocument
) -> tuple[str, list[weaviate_ops.Chunk], list[str]] | None:
    local_xlsx = _load_local_xlsx_texts(temp_doc)
    if local_xlsx is None:
        return None
    collection, texts, notes, file_size = local_xlsx
    chunks: list[weaviate_ops.Chunk] = []
    vectors = weaviate_ops.embed_texts(client, texts)
    for index, (text, vector) in enumerate(zip(texts, vectors)):
        chunks.append(
            {
                "text": text,
                "temp_doc_id": temp_doc.temp_document_id,
                "file_name": temp_doc.file_name,
                "file_path": temp_doc.file_path,
                "file_size": file_size,
                "i_page": 1,
                "i_chunk_on_doc": index,
                "i_chunk_on_page": index,
                "_additional": {"id": f"local-xlsx-{temp_doc.temp_document_id}-{index}", "vector": vector},
            }
        )
    return (collection, chunks, notes)


def _load_local_xlsx_texts(
    temp_doc: TempDocument,
    *,
    exclude_sheet_names: frozenset[str] = frozenset(),
) -> tuple[str, list[str], list[str], int] | None:
    if not temp_doc.file_name.lower().endswith(LOCAL_XLSX_SUFFIXES) or not temp_doc.file_path:
        return None
    path = Path(temp_doc.file_path)
    if not path.is_file():
        return None
    try:
        if should_stream_xlsx_chunks(path):
            texts = list(
                iter_xlsx_chunks(
                    path,
                    exclude_sheet_names=exclude_sheet_names,
                    allow_empty=bool(exclude_sheet_names),
                )
            )
            notes = ["xlsx 스트리밍 전처리 적용: 대형 flat 시트 청킹"]
        else:
            skip_report: list[SheetSkip] = []
            texts = extract_xlsx_chunks(
                path,
                skip_report=skip_report,
                exclude_sheet_names=exclude_sheet_names,
                allow_empty=bool(exclude_sheet_names),
            )
            notes = ["xlsx 전용 전처리 적용: 헤더-값 보존 청킹"]
            notes.extend(skip.note() for skip in skip_report)
        if exclude_sheet_names:
            notes.append(
                "SQL 시트 제외 후 VDB 잔여 청킹: "
                + ", ".join(sorted(exclude_sheet_names))
            )
    except XlsxPreprocessError as exc:
        return ("local_xlsx_preprocessor_failed", [], [f"xlsx 전용 전처리 실패: {exc}"], 0)
    file_size = path.stat().st_size
    return ("local_xlsx_preprocessor", texts, notes, file_size)


def _load_local_docx_chunks(
    client: httpx.Client, temp_doc: TempDocument
) -> tuple[str, list[weaviate_ops.Chunk], list[str]] | None:
    if not temp_doc.file_name.lower().endswith(".docx") or not temp_doc.file_path:
        return None
    path = Path(temp_doc.file_path)
    if not path.is_file():
        return None
    try:
        texts = extract_docx_chunks(path)
    except DocxPreprocessError as exc:
        return ("local_docx_preprocessor_failed", [], [f"docx 전용 전처리 실패: {exc}"])
    file_size = path.stat().st_size
    chunks: list[weaviate_ops.Chunk] = []
    vectors = weaviate_ops.embed_texts(client, texts)
    for index, (text, vector) in enumerate(zip(texts, vectors)):
        chunks.append(
            {
                "text": text,
                "temp_doc_id": temp_doc.temp_document_id,
                "file_name": temp_doc.file_name,
                "file_path": temp_doc.file_path,
                "file_size": file_size,
                "i_page": 1,
                "i_chunk_on_doc": index,
                "i_chunk_on_page": index,
                "_additional": {"id": f"local-docx-{temp_doc.temp_document_id}-{index}", "vector": vector},
            }
        )
    return ("local_docx_preprocessor", chunks, ["docx 전용 전처리 적용: 문단/표 보존 청킹"])


@app.get(
    "/health",
    summary="서비스 상태와 런타임 한도 확인",
    description=HEALTH_DESCRIPTION,
)
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "wf301-vdb-bridge",
        "mode": "commit" if settings.COMMIT_ENABLED else "dry_run",
        "commit_enabled": settings.COMMIT_ENABLED,
        "db_configured": bool(settings.DB_PASSWORD),
        "target_vdb_id": settings.TARGET_VDB_ID,
        "paths": [
            "/health",
            "/dry-run",
            "/commit",
            "/upload",
            "/documents",
            "/documents/delete",
            "/quota/check",
            "/search",
            "/file-sql/schema",
            "/file-sql/query",
        ],
        "file_sql_enabled": settings.FILE_SQL_ENABLED,
        "ttl_days": settings.TTL_DAYS,
        "quota": _quota_limits().model_dump(),
    }


@app.post(
    "/dry-run",
    response_model=DryRunResponse,
    summary="임시 문서 등록 계획 미리보기",
    description=DRY_RUN_DESCRIPTION,
)
def dry_run(req: BridgeRequest, request: Request) -> DryRunResponse:
    errors = _guard(req)
    plans: list[DocumentPlan] = []
    if errors and any("not allowed" in item for item in errors):
        return DryRunResponse(
            target_vdb_id=req.vdb_id,
            target_collection=settings.TARGET_VDB_COLLECTION,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            documents=[],
            errors=errors,
        )

    req = _owned_bridge_request(req, request_id=_request_id(request))
    session_id = _session_id(req)
    with httpx.Client() as client:
        for temp_doc in req.temp_documents:
            source_doc_key = f"temp:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            idempotency_key = f"wf301:{session_id}:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            gate_block = _preprocessor_gate_block_for_temp_doc(temp_doc)
            if gate_block is not None:
                plans.append(
                    DocumentPlan(
                        temp_document_id=temp_doc.temp_document_id,
                        file_name=temp_doc.file_name,
                        source_doc_key=source_doc_key,
                        source_collection=None,
                        chunk_count=0,
                        route=gate_block.route,
                        route_reason=gate_block.route_reason,
                        vector_dim=None,
                        idempotency_key=idempotency_key,
                        idempotency_status="blocked_oversized",
                        planned_document=None,
                        planned_upsert=None,
                        planned_vector_ids=0,
                        planned_139_objects=0,
                        notes=[gate_block.route_reason],
                    )
                )
                continue
            sql_decision = _sql_decision_for_temp_doc(temp_doc)
            sql_tables = (
                _sql_table_metadata(temp_doc, sql_decision)
                if sql_decision is not None and sql_decision.route == "sql"
                else []
            )
            selected_sheet_names = frozenset(
                profile.sheet_name
                for profile in (sql_decision.selected_sheets if sql_decision else ())
            )
            local_xlsx = _load_local_xlsx_texts(
                temp_doc,
                exclude_sheet_names=selected_sheet_names,
            )
            if sql_tables:
                collection = "session_sqlite"
                residual_texts: list[str] = []
                residual_notes: list[str] = []
                if local_xlsx is not None:
                    _xlsx_collection, residual_texts, residual_notes, _xlsx_size = local_xlsx
                notes = [
                    sql_decision.reason,
                    *(
                        json.dumps(profile.audit_dict(), ensure_ascii=False)
                        for profile in sql_decision.profiles
                    ),
                    *residual_notes,
                ]
                file_size_bytes = Path(temp_doc.file_path or "").stat().st_size
                chunk_count = len(residual_texts)
                vector_dim = None
            elif local_xlsx is not None:
                collection, texts, notes, file_size_bytes = local_xlsx
                chunk_count = len(texts)
                vector_dim = None
            else:
                collection, chunks, notes = _load_temp_chunks(client, temp_doc)
                chunk_count = len(chunks)
                vector_dim = weaviate_ops.first_vector_dim(chunks)
                file_size_bytes = weaviate_ops.max_file_size_bytes(chunks)
            route, route_reason = (
                ("sql", sql_decision.reason)
                if sql_tables and sql_decision is not None
                else _chunk_route(chunk_count)
            )
            if local_xlsx is not None and route != "blocked_oversized":
                timeout_gate = _xlsx_timeout_gate(chunk_count)
                if timeout_gate is not None:
                    route, route_reason = timeout_gate
            description = json.dumps(
                _description(
                    req,
                    source_doc_key=source_doc_key,
                    idempotency_key=idempotency_key,
                    temp_document_id=temp_doc.temp_document_id,
                    source_collection=collection,
                    expires_at=_expires_at(),
                    file_size_bytes=file_size_bytes,
                    storage_route=workbook_storage_route(
                        has_sql=bool(sql_tables),
                        vdb_chunk_count=chunk_count,
                    ),
                    route_reason=route_reason,
                    sql_tables=[table.model_dump() for table in sql_tables],
                ),
                ensure_ascii=False,
            )
            planned_doc = None
            planned_upsert = None
            if chunk_count or sql_tables:
                planned_doc = PlannedDocumentRow(
                    vdb_id=req.vdb_id,
                    org_file_name=temp_doc.file_name,
                    file_name=temp_doc.file_name,
                    description=description,
                )
                planned_upsert = PlannedUpsertRow(
                    vdb_id=req.vdb_id,
                    doc_id_placeholder="<document.id at commit>",
                    n_vectors=chunk_count,
                )
            plans.append(
                DocumentPlan(
                    temp_document_id=temp_doc.temp_document_id,
                    file_name=temp_doc.file_name,
                    source_doc_key=source_doc_key,
                    source_collection=collection,
                    chunk_count=chunk_count,
                    route=route,
                    route_reason=route_reason,
                    vector_dim=vector_dim,
                    idempotency_key=idempotency_key,
                    idempotency_status="new",
                    planned_document=planned_doc,
                    planned_upsert=planned_upsert,
                    planned_vector_ids=0,
                    planned_139_objects=chunk_count,
                    notes=notes,
                )
            )

    safe_log("dry_run_done", workflow_id=req.workflow_id, doc_count=len(plans), errors=len(errors))
    return DryRunResponse(
        target_vdb_id=req.vdb_id,
        target_collection=settings.TARGET_VDB_COLLECTION,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        documents=plans,
        errors=errors,
    )


@app.post(
    "/commit",
    response_model=PublicCommitResponse,
    summary="임시 문서를 공용 VDB 139에 정식 등록",
    description=COMMIT_DESCRIPTION,
)
def commit(req: BridgeRequest, request: Request) -> CommitResponse:
    return _commit_temp_documents(req, request_id=_request_id(request))


def _commit_temp_documents(req: BridgeRequest, *, request_id: str | None = None) -> CommitResponse:
    request_id = request_id or _request_id(None)
    if _guard(req) or not settings.COMMIT_ENABLED:
        return _commit_owned_temp_documents(req, request_id=request_id)
    session_id = _session_id(req)
    temp_ids = [item.temp_document_id for item in req.temp_documents]
    with _UPLOAD_OWNERSHIP.commit_guard(session_id, req.workflow_id, temp_ids):
        owned_req = _owned_bridge_request(req, request_id=request_id)
        return _commit_owned_temp_documents(owned_req, request_id=request_id)


def _commit_owned_temp_documents(req: BridgeRequest, *, request_id: str) -> CommitResponse:
    errors = _guard(req)
    if not settings.COMMIT_ENABLED:
        errors.append("commit stage is disabled")
    if errors:
        return CommitResponse(
            commit_enabled=settings.COMMIT_ENABLED,
            write_count=0,
            target_vdb_id=req.vdb_id,
            target_collection=settings.TARGET_VDB_COLLECTION,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            documents=[],
            errors=errors,
        )

    session_id = _session_id(req)
    user_id = req.user_id or settings.DEFAULT_USER_ID
    results: list[CommitDocumentResult] = []
    rollback: list[str] = []
    write_count = 0
    committed_count = 0
    skipped_duplicate_count = 0
    quota: QuotaSnapshot | None = None

    with httpx.Client() as client, ledger.ledger_connection() as conn:
        current_docs = ledger.list_session_documents(
            conn,
            workflow_id=req.workflow_id,
            session_id=session_id,
        )
        prepared: list[dict[str, Any]] = []
        for temp_doc in req.temp_documents:
            source_doc_key = f"temp:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            idempotency_key = f"wf301:{session_id}:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            gate_block = _preprocessor_gate_block_for_temp_doc(temp_doc)
            if gate_block is not None:
                prepared.append(
                    {
                        "temp_doc": temp_doc,
                        "source_doc_key": source_doc_key,
                        "idempotency_key": idempotency_key,
                        "collection": None,
                        "chunks": [],
                        "local_xlsx_texts": None,
                        "chunk_count": 0,
                        "route": gate_block.route,
                        "route_reason": gate_block.route_reason,
                        "notes": [gate_block.route_reason],
                        "vector_dim": None,
                        "file_size_bytes": gate_block.file_size_bytes,
                        "existing_doc_id": None,
                        "sql_decision": None,
                        "sql_tables": [],
                    }
                )
                continue
            sql_decision = _sql_decision_for_temp_doc(temp_doc)
            sql_tables = (
                _sql_table_metadata(temp_doc, sql_decision)
                if sql_decision is not None and sql_decision.route == "sql"
                else []
            )
            selected_sheet_names = frozenset(
                profile.sheet_name
                for profile in (sql_decision.selected_sheets if sql_decision else ())
            )
            local_xlsx = _load_local_xlsx_texts(
                temp_doc,
                exclude_sheet_names=selected_sheet_names,
            )
            local_xlsx_texts: list[str] | None = None
            if sql_tables:
                collection = "session_sqlite"
                local_xlsx_texts = []
                residual_notes: list[str] = []
                if local_xlsx is not None:
                    _xlsx_collection, local_xlsx_texts, residual_notes, _xlsx_size = local_xlsx
                notes = [
                    sql_decision.reason,
                    *(
                        json.dumps(profile.audit_dict(), ensure_ascii=False)
                        for profile in sql_decision.profiles
                    ),
                    *residual_notes,
                ]
                chunks = []
                chunk_count = len(local_xlsx_texts)
                vector_dim = None
                file_size_bytes = Path(temp_doc.file_path or "").stat().st_size
            elif local_xlsx is not None:
                collection, local_xlsx_texts, notes, file_size_bytes = local_xlsx
                chunks: list[weaviate_ops.Chunk] = []
                chunk_count = len(local_xlsx_texts)
                vector_dim = None
            else:
                collection, chunks, notes = _load_temp_chunks(client, temp_doc, enrich_visual=True)
                chunk_count = len(chunks)
                vector_dim = weaviate_ops.first_vector_dim(chunks)
                file_size_bytes = weaviate_ops.max_file_size_bytes(chunks)
            route, route_reason = (
                ("sql", sql_decision.reason)
                if sql_tables and sql_decision is not None
                else _chunk_route(chunk_count)
            )
            if local_xlsx is not None and route != "blocked_oversized":
                timeout_gate = _xlsx_timeout_gate(chunk_count)
                if timeout_gate is not None:
                    route, route_reason = timeout_gate
                    notes = [*notes, route_reason]
            existing_doc_id = (
                ledger.find_existing_document(conn, source_doc_key)
                if chunk_count or sql_tables
                else None
            )
            prepared.append(
                {
                    "temp_doc": temp_doc,
                    "source_doc_key": source_doc_key,
                    "idempotency_key": idempotency_key,
                    "collection": collection,
                    "chunks": chunks,
                    "local_xlsx_texts": local_xlsx_texts,
                    "chunk_count": chunk_count,
                    "route": route,
                    "route_reason": route_reason,
                    "notes": notes,
                    "vector_dim": vector_dim,
                    "file_size_bytes": file_size_bytes,
                    "existing_doc_id": existing_doc_id,
                    "sql_decision": sql_decision,
                    "sql_tables": sql_tables,
                }
            )

        new_items = [
            item
            for item in prepared
            if (item["chunk_count"] or item["sql_tables"])
            and item["existing_doc_id"] is None
            and item["route"] != "blocked_oversized"
        ]
        incoming_sizes = [int(item["file_size_bytes"] or 0) for item in new_items]
        quota = _quota_snapshot(
            current_docs,
            incoming_files=len(new_items),
            incoming_bytes=sum(incoming_sizes),
            incoming_file_sizes=incoming_sizes,
        )
        if not quota.allowed:
            return CommitResponse(
                commit_enabled=settings.COMMIT_ENABLED,
                write_count=0,
                target_vdb_id=req.vdb_id,
                target_collection=settings.TARGET_VDB_COLLECTION,
                workflow_id=req.workflow_id,
                app_session_id=req.app_session_id,
                documents=[],
                session_document_count=len(current_docs),
                quota=quota,
                errors=quota.violations,
            )

        for item in prepared:
            temp_doc = item["temp_doc"]
            source_doc_key = item["source_doc_key"]
            idempotency_key = item["idempotency_key"]
            collection = item["collection"]
            chunks = item["chunks"]
            local_xlsx_texts = item["local_xlsx_texts"]
            chunk_count = item["chunk_count"]
            route = item["route"]
            route_reason = item["route_reason"]
            notes = item["notes"]
            vector_dim = item["vector_dim"]
            file_size_bytes = int(item["file_size_bytes"] or 0)
            sql_decision = item["sql_decision"]
            sql_tables: list[SqlTableMetadata] = item["sql_tables"]
            if route == "blocked_oversized":
                results.append(
                    CommitDocumentResult(
                        temp_document_id=temp_doc.temp_document_id,
                        file_name=temp_doc.file_name,
                        source_doc_key=source_doc_key,
                        source_collection=collection,
                        chunk_count=chunk_count,
                        route=route,
                        route_reason=route_reason,
                        vector_dim=vector_dim,
                        status="blocked_oversized",
                        notes=[*notes, route_reason],
                    )
                )
                continue

            if not chunk_count and not sql_tables:
                results.append(
                    CommitDocumentResult(
                        temp_document_id=temp_doc.temp_document_id,
                        file_name=temp_doc.file_name,
                        source_doc_key=source_doc_key,
                        source_collection=collection,
                        chunk_count=0,
                        route=route,
                        route_reason=route_reason,
                        vector_dim=None,
                        status="no_chunks",
                        notes=notes,
                    )
                )
                continue

            existing_doc_id = item["existing_doc_id"]
            if existing_doc_id is not None:
                skipped_duplicate_count += 1
                results.append(
                    CommitDocumentResult(
                        temp_document_id=temp_doc.temp_document_id,
                        file_name=temp_doc.file_name,
                        source_doc_key=source_doc_key,
                        source_collection=collection,
                        document_id=existing_doc_id,
                        chunk_count=chunk_count,
                        route=route,
                        route_reason=route_reason,
                        vector_dim=vector_dim,
                        status="skipped_duplicate",
                        notes=["existing active document found by source_doc_key"],
                        sql_tables=sql_tables,
                    )
                )
                continue

            provisioned_logical_names: list[str] = []
            try:
                if sql_tables:
                    path = Path(temp_doc.file_path or "")
                    for table, profile in zip(
                        sql_tables,
                        sql_decision.selected_sheets,
                        strict=True,
                    ):
                        sheet_data = load_sql_sheet(path, profile)
                        provision_session_table(
                            session_id,
                            table.logical_name,
                            sheet_data.columns,
                            sheet_data.rows(),
                        )
                        provisioned_logical_names.append(table.logical_name)
                description = _description(
                    req,
                    source_doc_key=source_doc_key,
                    idempotency_key=idempotency_key,
                    temp_document_id=temp_doc.temp_document_id,
                    source_collection=collection,
                    expires_at=_expires_at(),
                    file_size_bytes=file_size_bytes,
                    storage_route=workbook_storage_route(
                        has_sql=bool(sql_tables),
                        vdb_chunk_count=chunk_count,
                    ),
                    route_reason=route_reason,
                    sql_tables=[table.model_dump() for table in sql_tables],
                )
                document_id = ledger.insert_document(
                    conn,
                    file_name=temp_doc.file_name,
                    description=description,
                    user_id=user_id,
                )
                upsert_id = ledger.insert_document_upsert(
                    conn,
                    document_id=document_id,
                    chunk_count=chunk_count,
                    user_id=user_id,
                )
                if local_xlsx_texts:
                    object_ids, vector_dim = weaviate_ops.copy_texts_to_target(
                        client,
                        local_xlsx_texts,
                        document_id=document_id,
                        file_name=temp_doc.file_name,
                        file_path=temp_doc.file_path or "",
                        file_size=file_size_bytes,
                        idempotency_key=idempotency_key,
                    )
                elif chunks:
                    object_ids = weaviate_ops.copy_chunks_to_target(
                        client,
                        chunks,
                        document_id=document_id,
                        file_name=temp_doc.file_name,
                        idempotency_key=idempotency_key,
                    )
                else:
                    object_ids = []
                if chunk_count:
                    session_wiki.mark_pages_stale(conn, req.workflow_id, session_id)
                conn.commit()
            except Exception:
                conn.rollback()
                for logical_name in provisioned_logical_names:
                    try:
                        drop_logical_table(session_id, logical_name)
                    except (FileSqlNotFoundError, FileSqlRejectedError):
                        pass
                raise

            write_count += 2 + len(object_ids)
            committed_count += 1
            if sql_tables and object_ids:
                rollback.append(
                    f"document_id={document_id}: set document/document_upsert is_active=0, "
                    f"drop session SQL tables {provisioned_logical_names}, "
                    f"and delete Weaviate object ids {object_ids}"
                )
            elif sql_tables:
                rollback.append(
                    f"document_id={document_id}: set document/document_upsert is_active=0 "
                    f"and drop session SQL tables {provisioned_logical_names}"
                )
            else:
                rollback.append(
                    f"document_id={document_id}: set document/document_upsert is_active=0 "
                    f"and delete Weaviate object ids {object_ids}"
                )
            results.append(
                CommitDocumentResult(
                    temp_document_id=temp_doc.temp_document_id,
                    file_name=temp_doc.file_name,
                    source_doc_key=source_doc_key,
                    source_collection=collection,
                    document_id=document_id,
                    document_upsert_id=upsert_id,
                    chunk_count=chunk_count,
                    route=route,
                    route_reason=route_reason,
                    vector_dim=vector_dim,
                    weaviate_object_ids=object_ids,
                    status=(
                        "committed_hybrid"
                        if sql_tables and object_ids
                        else "committed_sql"
                        if sql_tables
                        else "committed"
                    ),
                    notes=notes,
                    sql_tables=sql_tables,
                )
            )

        session_document_count = len(
            ledger.list_session_documents(
                conn,
                workflow_id=req.workflow_id,
                session_id=session_id,
            )
        )

    safe_log(
        "commit_done",
        request_id=request_id,
        principal_hash=_audit_hash(req.user_id),
        session_hash=_UPLOAD_OWNERSHIP.session_hash(session_id),
        temp_document_ids=[item.temp_document_id for item in req.temp_documents],
        decision="committed",
        workflow_id=req.workflow_id,
        docs=len(results),
        write_count=write_count,
    )
    return CommitResponse(
        commit_enabled=settings.COMMIT_ENABLED,
        write_count=write_count,
        target_vdb_id=req.vdb_id,
        target_collection=settings.TARGET_VDB_COLLECTION,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        documents=results,
        committed_count=committed_count,
        skipped_duplicate_count=skipped_duplicate_count,
        session_document_count=session_document_count,
        file_only_ready=bool(results) and not any(
            item.status in {"no_chunks", "blocked_oversized"} for item in results
        ),
        quota=quota,
        rollback_hint=rollback,
        errors=[item.route_reason for item in results if item.status == "blocked_oversized"],
    )


def _documents_from_rows(rows: list[dict[str, Any]]) -> list[SessionDocument]:
    return [SessionDocument(**row) for row in rows]


def _session_request(
    *,
    workflow_id: int,
    app_session_id: str,
    chat_id: str | None,
    user_id: int | None,
    vdb_id: int,
) -> SessionRequest:
    return SessionRequest(
        workflow_id=workflow_id,
        vdb_id=vdb_id,
        app_session_id=app_session_id,
        chat_id=chat_id,
        user_id=user_id,
    )


@app.post(
    "/upload",
    response_model=PublicUploadResponse,
    summary="파일을 세션 임시 VDB에 업로드",
    description=UPLOAD_DESCRIPTION,
)
def upload(
    files: list[UploadFile] = File(
        ...,
        description=(
            "업로드할 파일 목록입니다. 한 요청에 최대 10개까지 권장하며, "
            "각 파일은 workflow 301 파일 업로드 플러그인의 허용 확장자와 파일당 50MB 한도를 따릅니다."
        ),
    ),
    workflow_id: int = Form(
        ...,
        description="GenOS workflow ID입니다. wf301 파일 업로드 브리지는 301만 허용합니다.",
        examples=[WORKFLOW_ID_EXAMPLE],
    ),
    app_session_id: str | None = Form(
        None,
        description=(
            "포털 앱 세션 식별자입니다. chat_id가 없을 때 실제 세션 키로 사용됩니다. "
            "36자 이하의 영문/숫자/하이픈/언더스코어 조합을 권장합니다."
        ),
        json_schema_extra=SESSION_KEY_SCHEMA,
        examples=[SESSION_KEY_EXAMPLE],
    ),
    chat_id: str | None = Form(
        None,
        description=(
            "채팅 세션 식별자입니다. 값이 있으면 app_session_id보다 우선해 실제 세션 키가 됩니다. "
            "업로드부터 커밋, 검색, 삭제까지 같은 값을 유지하세요."
        ),
        json_schema_extra=SESSION_KEY_SCHEMA,
        examples=[SESSION_KEY_EXAMPLE],
    ),
    user_id: int | None = Form(
        None,
        description="GenOS 원장/임시 VDB 호출에 전달할 사용자 ID입니다. 생략하면 서비스 기본 user_id를 사용합니다.",
    ),
    vdb_id: int = Form(
        default=settings.TARGET_VDB_ID,
        description="정식 등록 대상 VDB ID입니다. 기본값은 공용 파일 검색 VDB인 139이며 다른 값은 거부됩니다.",
        examples=[TARGET_VDB_EXAMPLE],
    ),
) -> UploadResponse:
    session_value = chat_id or app_session_id or ""
    app_session_value = app_session_id or session_value
    req = _session_request(
        workflow_id=workflow_id,
        app_session_id=app_session_value,
        chat_id=chat_id,
        user_id=user_id,
        vdb_id=vdb_id,
    )
    errors = _session_guard(req)
    if not session_value:
        errors.append("app_session_id or chat_id is required")
    if not settings.COMMIT_ENABLED:
        errors.append("commit stage is disabled")
    if errors:
        return UploadResponse(
            target_vdb_id=vdb_id,
            workflow_id=workflow_id,
            app_session_id=app_session_value,
            session_id=session_value,
            errors=errors,
        )

    with ledger.ledger_connection() as conn:
        config = upload_adapter.load_file_upload_config(conn, workflow_id=workflow_id)
        extension_errors = upload_adapter.validate_extensions(files, config.allowed_extensions)
        if extension_errors:
            return UploadResponse(
                target_vdb_id=vdb_id,
                workflow_id=workflow_id,
                app_session_id=app_session_value,
                session_id=session_value,
                errors=extension_errors,
            )
        incoming_sizes = [upload_adapter.upload_file_size(file) for file in files]
        current_docs = ledger.list_session_documents(
            conn,
            workflow_id=workflow_id,
            session_id=session_value,
        )
        quota = _quota_snapshot(
            current_docs,
            incoming_files=len(files),
            incoming_bytes=sum(incoming_sizes),
            incoming_file_sizes=incoming_sizes,
        )
        if not quota.allowed:
            return UploadResponse(
                target_vdb_id=vdb_id,
                workflow_id=workflow_id,
                app_session_id=app_session_value,
                session_id=session_value,
                quota=quota,
                errors=quota.violations,
            )
        upload_blocks = upload_adapter.blocked_external_preprocessor_uploads(files)
        if upload_blocks:
            return UploadResponse(
                target_vdb_id=vdb_id,
                workflow_id=workflow_id,
                app_session_id=app_session_value,
                session_id=session_value,
                quota=quota,
                blocked_uploads=_blocked_upload_models(upload_blocks),
                errors=[block.route_reason for block in upload_blocks],
            )

        saved_documents: list[upload_adapter.SavedTempDocument] = []
        temp_document_ids: list[int] = []
        try:
            with httpx.Client() as client:
                temp_vdb = upload_adapter.create_temp_vdb_index(
                    client,
                    app_session_id=session_value,
                    lifespan_days=config.lifespan_days,
                    user_id=user_id,
                )
                temp_document_ids = upload_adapter.insert_temp_documents(
                    conn,
                    temp_vdb_index_id=temp_vdb.temp_vdb_index_id,
                    files=files,
                )
                saved_documents = upload_adapter.save_temp_documents(
                    files,
                    temp_document_ids=temp_document_ids,
                    destination_dir=_UPLOAD_OWNERSHIP.session_root(
                        Path(settings.TEMP_DOCUMENT_DIR), session_value
                    ),
                )
                saved_blocks = upload_adapter.blocked_saved_external_preprocessor_documents(
                    saved_documents
                )
                if saved_blocks:
                    upload_adapter.cleanup_saved_documents(saved_documents)
                    upload_adapter.deactivate_temp_documents(conn, temp_document_ids=temp_document_ids)
                    conn.commit()
                    return UploadResponse(
                        target_vdb_id=vdb_id,
                        workflow_id=workflow_id,
                        app_session_id=app_session_value,
                        session_id=session_value,
                        temp_vdb_index_id=temp_vdb.temp_vdb_index_id,
                        temp_vdb_index=temp_vdb.temp_vdb_index,
                        quota=quota,
                        blocked_uploads=_blocked_upload_models(saved_blocks),
                        errors=[block.route_reason for block in saved_blocks],
                    )
                expires_at = datetime.now(timezone.utc) + timedelta(days=config.lifespan_days)
                for item in saved_documents:
                    _UPLOAD_OWNERSHIP.register(
                        root_dir=Path(settings.TEMP_DOCUMENT_DIR),
                        session_id=session_value,
                        workflow_id=workflow_id,
                        temp_document_id=item.temp_document_id,
                        file_name=item.file_name,
                        file_path=Path(item.file_path),
                        expires_at=expires_at,
                    )
                external_preprocessor_documents = [
                    item
                    for item in saved_documents
                    if upload_adapter.requires_external_preprocessor(item)
                ]
                if external_preprocessor_documents:
                    try:
                        upload_adapter.run_preprocessor(
                            client,
                            temp_vdb_index=temp_vdb.temp_vdb_index,
                            config=config,
                            saved_documents=external_preprocessor_documents,
                            user_id=user_id,
                        )
                    except upload_adapter.PreprocessorRunError as exc:
                        # fail-closed: facade가 200으로 감싼 전처리 실패를 조용한 성공으로
                        # 커밋하지 않는다. temp 문서를 정리하고 명시 실패를 반환한다.
                        # (bounded 재시도는 별건 — worker 크래시루프 시 무력하므로 여기선 하지 않음)
                        failure_blocks = upload_adapter.preprocessor_failure_blocks(
                            exc, external_preprocessor_documents
                        )
                        upload_adapter.cleanup_saved_documents(saved_documents)
                        upload_adapter.deactivate_temp_documents(
                            conn, temp_document_ids=temp_document_ids
                        )
                        conn.commit()
                        safe_log(
                            "upload_preprocess_failed",
                            request_id=uuid.uuid4().hex,
                            principal_hash=_audit_hash(user_id),
                            session_hash=_UPLOAD_OWNERSHIP.session_hash(session_value),
                            temp_document_ids=temp_document_ids,
                            decision="preprocess_failed",
                            workflow_id=workflow_id,
                            reason=exc.reason,
                        )
                        return UploadResponse(
                            target_vdb_id=vdb_id,
                            workflow_id=workflow_id,
                            app_session_id=app_session_value,
                            session_id=session_value,
                            temp_vdb_index_id=temp_vdb.temp_vdb_index_id,
                            temp_vdb_index=temp_vdb.temp_vdb_index,
                            quota=quota,
                            blocked_uploads=_blocked_upload_models(failure_blocks),
                            errors=[block.route_reason for block in failure_blocks],
                        )
            conn.commit()
        except (OSError, RuntimeError, httpx.HTTPError):
            conn.rollback()
            upload_adapter.cleanup_saved_documents(saved_documents)
            upload_adapter.deactivate_temp_documents(conn, temp_document_ids=temp_document_ids)
            conn.commit()
            raise

    safe_log(
        "upload_done",
        request_id=uuid.uuid4().hex,
        principal_hash=_audit_hash(user_id),
        session_hash=_UPLOAD_OWNERSHIP.session_hash(session_value),
        temp_document_ids=temp_document_ids,
        decision="registered",
        workflow_id=workflow_id,
        docs=len(files),
    )
    uploaded_temp_documents = [
        UploadedTempDocument(
            temp_document_id=item.temp_document_id,
            file_name=item.file_name,
            file_path=item.file_path,
        )
        for item in saved_documents
    ]
    commit_response = _commit_temp_documents(
        BridgeRequest(
            workflow_id=workflow_id,
            vdb_id=vdb_id,
            app_session_id=app_session_value,
            chat_id=chat_id,
            user_id=user_id,
            temp_documents=[
                TempDocument(
                    temp_document_id=item.temp_document_id,
                    file_name=item.file_name,
                    file_path=item.file_path,
                )
                for item in saved_documents
            ],
        )
    )
    upload_errors = list(commit_response.errors)
    if not commit_response.file_only_ready and not upload_errors:
        upload_errors.append("commit completed without searchable document chunks")
    return UploadResponse(
        target_vdb_id=vdb_id,
        workflow_id=workflow_id,
        app_session_id=app_session_value,
        session_id=session_value,
        temp_vdb_index_id=temp_vdb.temp_vdb_index_id,
        temp_vdb_index=temp_vdb.temp_vdb_index,
        temp_documents=uploaded_temp_documents,
        commit=commit_response,
        quota=quota,
        errors=upload_errors,
    )


@app.get(
    "/documents",
    response_model=PublicDocumentsResponse,
    summary="세션에 등록된 검색 가능 문서 목록 조회",
    description=DOCUMENTS_DESCRIPTION,
)
def documents(
    workflow_id: int = Query(
        ...,
        description="GenOS workflow ID입니다. wf301 파일 업로드 브리지는 301만 허용합니다.",
        examples=[WORKFLOW_ID_EXAMPLE],
    ),
    app_session_id: str = Query(
        ...,
        description="포털 앱 세션 식별자입니다. chat_id가 없으면 이 값으로 등록 문서를 조회합니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
        examples=[SESSION_KEY_EXAMPLE],
    ),
    chat_id: str | None = Query(
        None,
        description="채팅 세션 식별자입니다. 값이 있으면 app_session_id보다 우선해 실제 조회 세션 키가 됩니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
        examples=[SESSION_KEY_EXAMPLE],
    ),
    user_id: int | None = Query(
        None,
        description="호출 사용자 ID입니다. 문서 목록 조회 자체는 세션 키와 workflow_id를 기준으로 수행됩니다.",
    ),
    vdb_id: int = Query(
        settings.TARGET_VDB_ID,
        description="조회 대상 VDB ID입니다. 기본값은 139이며 다른 값은 거부됩니다.",
        examples=[TARGET_VDB_EXAMPLE],
    ),
) -> DocumentsResponse:
    req = _session_request(
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        chat_id=chat_id,
        user_id=user_id,
        vdb_id=vdb_id,
    )
    errors = _session_guard(req)
    session_id = _session_id(req)
    if errors:
        return DocumentsResponse(
            target_vdb_id=vdb_id,
            workflow_id=workflow_id,
            app_session_id=app_session_id,
            session_id=session_id,
            documents=[],
            errors=errors,
        )
    with ledger.ledger_connection() as conn:
        rows = ledger.list_session_documents(
            conn,
            workflow_id=workflow_id,
            session_id=session_id,
        )
    return DocumentsResponse(
        target_vdb_id=vdb_id,
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        session_id=session_id,
        documents=_documents_from_rows(rows),
    )


@app.post(
    "/documents/delete",
    response_model=DeleteDocumentResponse,
    summary="세션 등록 문서 1건 삭제",
    description=DELETE_DESCRIPTION,
)
@app.delete(
    "/documents/delete",
    response_model=DeleteDocumentResponse,
    summary="세션 등록 문서 1건 삭제(DELETE)",
    description=DELETE_DELETE_DESCRIPTION,
)
def delete_document(req: DeleteDocumentRequest) -> DeleteDocumentResponse:
    errors = _session_guard(req)
    session_id = _session_id(req)
    if req.document_id is None and req.temp_document_id is None:
        errors.append("document_id or temp_document_id is required")
    if errors:
        return delete_ops.error_response(
            req,
            session_id=session_id,
            status="rejected",
            errors=errors,
        )
    response = delete_ops.delete_session_document(
        req,
        session_id=session_id,
        user_id=req.user_id or settings.DEFAULT_USER_ID,
    )
    if response.status in {"deleted", "already_deleted"} and response.temp_document_id is not None:
        _UPLOAD_OWNERSHIP.remove(session_id, response.temp_document_id)
    return response


@app.get(
    "/quota/check",
    response_model=QuotaCheckResponse,
    summary="세션 업로드 쿼터 상태 확인",
    description=QUOTA_DESCRIPTION,
)
def quota_check(
    workflow_id: int = Query(
        ...,
        description="GenOS workflow ID입니다. wf301 파일 업로드 브리지는 301만 허용합니다.",
        examples=[WORKFLOW_ID_EXAMPLE],
    ),
    app_session_id: str = Query(
        ...,
        description="포털 앱 세션 식별자입니다. chat_id가 없으면 이 값으로 현재 세션 쿼터를 계산합니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
        examples=[SESSION_KEY_EXAMPLE],
    ),
    chat_id: str | None = Query(
        None,
        description="채팅 세션 식별자입니다. 값이 있으면 app_session_id보다 우선해 실제 쿼터 계산 세션 키가 됩니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
        examples=[SESSION_KEY_EXAMPLE],
    ),
    user_id: int | None = Query(
        None,
        description="호출 사용자 ID입니다. 쿼터 계산은 세션 키와 workflow_id를 기준으로 수행됩니다.",
    ),
    vdb_id: int = Query(
        settings.TARGET_VDB_ID,
        description="쿼터 확인 대상 VDB ID입니다. 기본값은 139이며 다른 값은 거부됩니다.",
        examples=[TARGET_VDB_EXAMPLE],
    ),
) -> QuotaCheckResponse:
    req = _session_request(
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        chat_id=chat_id,
        user_id=user_id,
        vdb_id=vdb_id,
    )
    errors = _session_guard(req)
    session_id = _session_id(req)
    rows: list[dict[str, Any]] = []
    if not errors:
        with ledger.ledger_connection() as conn:
            rows = ledger.list_session_documents(
                conn,
                workflow_id=workflow_id,
                session_id=session_id,
            )
    return QuotaCheckResponse(
        target_vdb_id=vdb_id,
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        session_id=session_id,
        quota=_quota_snapshot(rows),
        errors=errors,
    )


def _is_empty_page_marker_text(text: str) -> bool:
    """외부 PDF 전처리기의 빈 페이지 자리표시 텍스트인지 판정한다."""
    normalized = text.strip().lower()
    return bool(normalized) and normalized == settings.PDF_EMPTY_PAGE_MARKER.lower()


def _hit_provenance(hit: dict[str, Any]) -> dict[str, Any]:
    summary = hit.get("summary")
    if isinstance(summary, str) and summary:
        try:
            decoded = json.loads(summary)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            pass
    return {}


def _is_empty_page_marker_hit(text: str, provenance: dict[str, Any]) -> bool:
    return bool(provenance.get("empty_page")) or _is_empty_page_marker_text(text)


def _context_from_hits(
    hits: list[dict[str, Any]],
    char_limit: int = settings.SEARCH_CONTEXT_CHAR_LIMIT,
) -> tuple[str, list[FileSource], list[EmptyPageSource]]:
    lines: list[str] = []
    sources: list[FileSource] = []
    empty_pages: list[EmptyPageSource] = []
    used_chars = 0
    # 같은 문서/페이지가 VLM 시각 채널 청크로도 조회됐는지 먼저 수집한다.
    # 빈 페이지 자리표시를 visual_content_not_processed로 잘못 표시하지 않기 위한 판정 근거.
    vlm_pages: set[tuple[int, Any]] = set()
    for hit in hits:
        provenance = _hit_provenance(hit)
        if str(provenance.get("source_channel") or "") == pdf_vlm.SOURCE_CHANNEL:
            vlm_pages.add((int(hit.get("doc_id") or 0), hit.get("i_page")))
    index = 0
    for hit in hits:
        text = str(hit.get("text") or "").strip()
        if not text:
            continue
        doc_id = int(hit.get("doc_id") or 0)
        file_name = str(hit.get("file_name") or "")
        provenance = _hit_provenance(hit)
        additional = hit.get("_additional") or {}
        if _is_empty_page_marker_hit(text, provenance):
            # 텍스트 없는 페이지의 자리표시 청크는 근거가 아니므로 검색 컨텍스트에서 제외한다.
            visual_processed = bool(provenance.get("visual_processed")) or (
                (doc_id, hit.get("i_page")) in vlm_pages
            )
            empty_pages.append(
                EmptyPageSource(
                    document_id=doc_id,
                    file_name=file_name,
                    chunk_id=additional.get("id"),
                    i_page=hit.get("i_page"),
                    status=settings.PDF_EMPTY_PAGE_STATUS_PROCESSED
                    if visual_processed
                    else settings.PDF_EMPTY_PAGE_STATUS_NOT_PROCESSED,
                )
            )
            continue
        remaining = char_limit - used_chars
        if remaining <= 0:
            break
        clipped = text[:remaining]
        used_chars += len(clipped)
        source_channel = str(provenance.get("source_channel") or "native_text")
        label = " [image-derived extraction]" if source_channel == pdf_vlm.SOURCE_CHANNEL else ""
        index += 1
        lines.append(f"[{index}] {file_name} (document_id={doc_id}){label}\n{clipped}")
        sources.append(
            FileSource(
                document_id=doc_id,
                file_name=file_name,
                chunk_id=additional.get("id"),
                i_page=hit.get("i_page"),
                i_chunk_on_doc=hit.get("i_chunk_on_doc"),
                distance=additional.get("distance"),
                source_channel=source_channel,
                visual_model=provenance.get("visual_model"),
            )
        )
    return "\n\n".join(lines), sources, empty_pages


def _join_file_contexts(wiki_context: str, vdb_context: str) -> str:
    if wiki_context and vdb_context:
        return f"{wiki_context}\n\n{vdb_context}"
    return wiki_context or vdb_context


def _sql_sources_from_rows(rows: list[dict[str, Any]]) -> list[FileSqlSource]:
    sources: list[FileSqlSource] = []
    for row in rows:
        if row.get("storage_route") not in {"sql", "hybrid"}:
            continue
        for raw_table in row.get("sql_tables") or []:
            if not isinstance(raw_table, dict):
                continue
            sources.append(
                FileSqlSource(
                    document_id=int(row["document_id"]),
                    file_name=str(row.get("file_name") or ""),
                    **raw_table,
                )
            )
    return sources


def _require_owned_sql_source(req: FileSqlSchemaRequest) -> FileSqlSource:
    errors = _session_guard(req)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    session_id = _session_id(req)
    with ledger.ledger_connection() as conn:
        sources = _sql_sources_from_rows(
            ledger.list_session_documents(
                conn,
                workflow_id=req.workflow_id,
                session_id=session_id,
            )
        )
    source = next(
        (item for item in sources if item.logical_name == req.logical_name),
        None,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="file SQL table not found for session")
    return source


@app.post("/file-sql/schema", response_model=FileSqlSchemaResponse)
def file_sql_schema(req: FileSqlSchemaRequest) -> FileSqlSchemaResponse:
    _require_owned_sql_source(req)
    try:
        schema = describe_schema_for_llm(_session_id(req), req.logical_name)
    except (FileSqlNotFoundError, FileSqlRejectedError) as exc:
        raise HTTPException(status_code=404, detail="file SQL schema unavailable") from exc
    return FileSqlSchemaResponse(
        logical_name=schema.logical_name,
        columns=[
            FileSqlColumn(query_name=query, source_name=source)
            for source, query in zip(
                schema.source_columns,
                schema.query_columns,
                strict=True,
            )
        ],
        llm_description=schema.llm_description,
    )


@app.post("/file-sql/query", response_model=FileSqlQueryResponse)
def file_sql_query(req: FileSqlQueryRequest) -> FileSqlQueryResponse:
    _require_owned_sql_source(req)
    try:
        result = run_scoped_query(_session_id(req), req.logical_name, req.sql)
    except (FileSqlNotFoundError, FileSqlRejectedError) as exc:
        raise HTTPException(status_code=400, detail="file SQL query rejected") from exc
    return FileSqlQueryResponse(
        logical_name=req.logical_name,
        columns=list(result.columns),
        rows=[list(row) for row in result.rows],
        row_count=len(result.rows),
    )


@app.post(
    "/search",
    response_model=PublicSearchResponse,
    summary="세션 등록 문서 벡터 검색",
    description=SEARCH_DESCRIPTION,
)
def search(req: SearchRequest) -> SearchResponse:
    errors = _session_guard(req)
    session_id = _session_id(req)
    if errors:
        return SearchResponse(
            target_vdb_id=req.vdb_id,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            session_id=session_id,
            question=req.question,
            document_count=0,
            result_count=0,
            file_context="",
            file_sources=[],
            errors=errors,
        )
    wiki_context = ""
    wiki_sources: list[FileSource] = []
    with ledger.ledger_connection() as conn:
        rows = ledger.list_session_documents(
            conn,
            workflow_id=req.workflow_id,
            session_id=session_id,
        )
        vdb_rows = [row for row in rows if row.get("storage_route") != "sql"]
        if settings.WIKI_ENABLED and vdb_rows:
            pages = session_wiki.read_ready_pages(
                conn, req.workflow_id, session_id, vdb_rows
            )
            if pages:
                wiki_context, wiki_sources = session_wiki.context_from_pages(
                    pages,
                    min(settings.WIKI_CONTEXT_CHAR_LIMIT, settings.SEARCH_CONTEXT_CHAR_LIMIT),
                )
            elif session_wiki.should_trigger(vdb_rows):
                session_wiki.trigger_compile_async(req.workflow_id, session_id)
    sql_sources = _sql_sources_from_rows(rows)
    doc_ids = [
        int(row["document_id"])
        for row in rows
        if row.get("storage_route") != "sql"
    ]
    if not rows:
        return SearchResponse(
            target_vdb_id=req.vdb_id,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            session_id=session_id,
            question=req.question,
            document_count=0,
            result_count=0,
            file_context="",
            file_sources=[],
        )
    if not doc_ids:
        return SearchResponse(
            target_vdb_id=req.vdb_id,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            session_id=session_id,
            question=req.question,
            document_count=len(rows),
            result_count=0,
            file_context="",
            file_sources=[],
            sql_available=bool(sql_sources),
            sql_sources=sql_sources,
        )
    with httpx.Client() as client:
        vector = weaviate_ops.embed_text(client, req.question)
        hits = weaviate_ops.search_target_chunks(
            client,
            vector=vector,
            doc_ids=doc_ids,
            limit=req.limit or settings.SEARCH_LIMIT,
        )
    remaining_chars = max(settings.SEARCH_CONTEXT_CHAR_LIMIT - len(wiki_context) - (2 if wiki_context else 0), 0)
    vdb_context, vdb_sources, empty_page_sources = _context_from_hits(hits, remaining_chars)
    file_context = _join_file_contexts(wiki_context, vdb_context)
    file_sources = [*wiki_sources, *vdb_sources]
    return SearchResponse(
        target_vdb_id=req.vdb_id,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        session_id=session_id,
        question=req.question,
        document_count=len(rows),
        result_count=len(hits),
        file_context=file_context,
        file_sources=file_sources,
        sql_available=bool(sql_sources),
        sql_sources=sql_sources,
        empty_page_sources=empty_page_sources,
    )
