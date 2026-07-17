"""Request and response models for the bridge API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from . import settings

WORKFLOW_ID_SCHEMA = {"examples": [301]}
TARGET_VDB_SCHEMA = {"examples": [settings.TARGET_VDB_ID]}
SESSION_KEY_SCHEMA = {
    "maxLength": 36,
    "pattern": "^[A-Za-z0-9_-]{1,36}$",
    "examples": ["puc-004928"],
}
QUESTION_SCHEMA = {"examples": ["업로드한 파일의 시장 규모 요약해줘"]}
LIMIT_SCHEMA = {"examples": [5]}


class TempDocument(BaseModel):
    temp_document_id: int = Field(description="/upload 또는 GenOS 임시 문서 테이블에서 받은 임시 문서 ID입니다.")
    file_name: str = Field(description="하위호환용 표시값입니다. commit은 업로드 세션 원장의 서버측 파일명을 사용합니다.")
    file_path: str | None = Field(
        default=None,
        description="하위호환용 값입니다. commit은 이 경로를 신뢰하지 않고 세션 원장의 canonical 경로를 사용합니다.",
    )


class UploadedTempDocument(BaseModel):
    temp_document_id: int = Field(description="/commit에 전달해야 하는 임시 문서 ID입니다.")
    file_name: str = Field(description="업로드된 파일명입니다.")
    file_path: str = Field(description="서비스가 저장한 임시 파일 경로입니다. 응답 확인과 장애 분석용으로 제공합니다.")


class BlockedUpload(BaseModel):
    file_name: str = Field(description="차단된 업로드 파일명입니다.")
    route: Literal["blocked_oversized", "preprocess_failed"] = Field(
        description=(
            "업로드 라우팅 판정입니다. blocked_oversized=위임 전 크기/페이지 차단, "
            "preprocess_failed=facade 위임 후 전처리 실패(조용한 성공 차단)."
        )
    )
    route_reason: str = Field(description="preprocessor 위임 차단 또는 실패 근거입니다.")
    file_size_bytes: int = Field(default=0, description="차단된 파일의 크기 byte입니다.")
    message: str = Field(description="사용자에게 표시할 수 있는 안전한 실패 사유입니다.")


class BridgeRequest(BaseModel):
    """wf301 Python Step thin payload."""

    workflow_id: int = Field(
        description="GenOS workflow ID입니다. wf301 파일 업로드 브리지는 301만 허용합니다.",
        json_schema_extra=WORKFLOW_ID_SCHEMA,
    )
    vdb_id: int = Field(
        default=settings.TARGET_VDB_ID,
        description="정식 등록 대상 VDB ID입니다. 기본값은 공용 파일 검색 VDB인 139이며 다른 값은 거부됩니다.",
        json_schema_extra=TARGET_VDB_SCHEMA,
    )
    app_session_id: str = Field(
        description=(
            "포털 앱 세션 식별자입니다. chat_id가 없을 때 실제 세션 키로 사용됩니다. "
            "36자 이하의 영문/숫자/하이픈/언더스코어 조합을 권장합니다."
        ),
        json_schema_extra=SESSION_KEY_SCHEMA,
    )
    chat_id: str | None = Field(
        default=None,
        description="채팅 세션 식별자입니다. 값이 있으면 app_session_id보다 우선해 실제 세션 키가 됩니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
    )
    user_id: int | None = Field(default=None, description="GenOS 문서 원장과 임시 VDB 호출에 전달할 사용자 ID입니다.")
    temp_documents: list[TempDocument] = Field(
        default_factory=list,
        description="/upload 응답의 temp_document_id를 전달합니다. 서버가 호출 세션 원장에서 소유권과 canonical 메타데이터를 확인합니다.",
    )


class SessionRequest(BaseModel):
    workflow_id: int = Field(
        description="GenOS workflow ID입니다. wf301 파일 업로드 브리지는 301만 허용합니다.",
        json_schema_extra=WORKFLOW_ID_SCHEMA,
    )
    vdb_id: int = Field(
        default=settings.TARGET_VDB_ID,
        description="조회/검색/삭제 대상 VDB ID입니다. 기본값은 139이며 다른 값은 거부됩니다.",
        json_schema_extra=TARGET_VDB_SCHEMA,
    )
    app_session_id: str = Field(
        description="포털 앱 세션 식별자입니다. chat_id가 없을 때 실제 세션 키로 사용됩니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
    )
    chat_id: str | None = Field(
        default=None,
        description="채팅 세션 식별자입니다. 값이 있으면 app_session_id보다 우선해 실제 세션 키가 됩니다.",
        json_schema_extra=SESSION_KEY_SCHEMA,
    )
    user_id: int | None = Field(default=None, description="호출 사용자 ID입니다. 생략하면 서비스 기본 user_id를 사용합니다.")


class SearchRequest(SessionRequest):
    question: str = Field(
        description="commit된 세션 문서에서 검색할 사용자 질문입니다.",
        json_schema_extra=QUESTION_SCHEMA,
    )
    limit: int | None = Field(
        default=None,
        description="검색 결과 최대 개수입니다. 생략하면 서비스 기본 SEARCH_LIMIT을 사용합니다.",
        json_schema_extra=LIMIT_SCHEMA,
    )


class FileSqlSchemaRequest(SessionRequest):
    logical_name: str = Field(
        description="/search의 sql_sources에서 받은 세션 소유 논리 테이블 이름입니다."
    )


class FileSqlQueryRequest(FileSqlSchemaRequest):
    sql: str = Field(
        description="SELECT/CTE-only 파일 질의입니다. data 논리 테이블만 참조할 수 있습니다."
    )


class DeleteDocumentRequest(SessionRequest):
    document_id: int | None = Field(default=None, description="/documents에서 확인한 정식 GenOS document ID입니다.")
    temp_document_id: int | None = Field(default=None, description="/upload 응답 또는 /documents 목록에 있는 임시 문서 ID입니다.")


class PlannedDocumentRow(BaseModel):
    vdb_id: int = Field(description="/commit 시 document row에 기록될 대상 VDB ID입니다.")
    org_file_name: str = Field(description="원본 파일명입니다.")
    file_name: str = Field(description="등록 파일명입니다.")
    description: str = Field(description="세션, temp_document_id, TTL, idempotency key 등을 담은 document description JSON입니다.")
    is_active: int = 1


class PlannedUpsertRow(BaseModel):
    vdb_id: int = Field(description="/commit 시 document_upsert row에 기록될 대상 VDB ID입니다.")
    doc_id_placeholder: str = Field(description="commit 시 생성되는 document.id가 들어갈 자리 표시자입니다.")
    status_on_complete: str = Field(default=settings.JS_COMPLETE, description="commit 완료 시 document_upsert에 기록할 상태값입니다.")
    n_vectors: int = Field(description="등록될 벡터 청크 수입니다.")
    note: str = "preprocessor_id/serving_id/serving_rev_id are commit-stage required"


class DocumentPlan(BaseModel):
    temp_document_id: int = Field(description="계획 대상 임시 문서 ID입니다.")
    file_name: str = Field(description="계획 대상 파일명입니다.")
    source_doc_key: str = Field(description="중복 등록 방지와 원본 추적에 사용하는 source document key입니다.")
    source_collection: str | None = Field(description="임시 청크를 읽은 temp VDB 컬렉션명입니다.")
    chunk_count: int = Field(description="읽어온 청크 수입니다. 0이면 commit해도 등록할 본문이 없습니다.")
    route: Literal["vdb", "vdb_large", "blocked_oversized", "sql"] = Field(
        default="vdb",
        description="청크 수 기반 등록 경로 판정입니다. vdb_large는 경고만, blocked_oversized는 commit 차단입니다.",
    )
    route_reason: str = Field(default="chunk_count is within VDB route limits", description="route 판정 근거입니다.")
    vector_dim: int | None = Field(description="임시 청크 vector 차원입니다. vector가 없으면 null입니다.")
    idempotency_key: str = Field(description="같은 세션/문서의 중복 commit을 방지하는 키입니다.")
    idempotency_status: str = Field(description="계획 시점의 중복 상태입니다.")
    planned_document: PlannedDocumentRow | None = Field(description="commit 시 생성될 document row 계획입니다. 청크가 없으면 null입니다.")
    planned_upsert: PlannedUpsertRow | None = Field(description="commit 시 생성될 document_upsert row 계획입니다. 청크가 없으면 null입니다.")
    planned_vector_ids: int = Field(description="계획 단계에서 생성될 vector id 수입니다. dry-run은 쓰지 않으므로 보통 0입니다.")
    planned_139_objects: int = Field(description="commit 시 VDB 139에 복사될 객체 수입니다.")
    notes: list[str] = Field(default_factory=list)


class DryRunResponse(BaseModel):
    mode: str = Field(default="dry_run", description="응답 모드입니다. dry-run은 쓰기를 수행하지 않습니다.")
    commit_enabled: bool = Field(default=settings.COMMIT_ENABLED, description="현재 서비스에서 commit 쓰기가 활성화되어 있는지 여부입니다.")
    write_count: int = Field(default=0, description="dry-run은 실제 쓰기를 하지 않으므로 항상 0입니다.")
    target_vdb_id: int = Field(description="정식 등록 대상 VDB ID입니다.")
    target_collection: str = Field(description="정식 등록 대상 Weaviate 컬렉션명입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    documents: list[DocumentPlan] = Field(description="문서별 commit 계획입니다.")
    errors: list[str] = Field(default_factory=list)


class CommitDocumentResult(BaseModel):
    temp_document_id: int = Field(description="commit 대상 임시 문서 ID입니다.")
    file_name: str = Field(description="commit 대상 파일명입니다.")
    source_doc_key: str = Field(description="중복 등록 확인에 사용한 source document key입니다.")
    source_collection: str | None = Field(description="임시 청크를 읽은 source collection입니다.")
    document_id: int | None = Field(default=None, description="생성되었거나 이미 존재하던 GenOS document ID입니다.")
    document_upsert_id: int | None = Field(default=None, description="신규 commit 시 생성된 document_upsert ID입니다.")
    chunk_count: int = Field(description="등록 또는 중복 확인된 청크 수입니다.")
    route: Literal["vdb", "vdb_large", "blocked_oversized", "sql"] = Field(
        default="vdb",
        description="청크 수 기반 등록 경로 판정입니다. blocked_oversized면 해당 문서는 VDB에 등록되지 않습니다.",
    )
    route_reason: str = Field(default="chunk_count is within VDB route limits", description="route 판정 근거입니다.")
    vector_dim: int | None = Field(description="청크 vector 차원입니다.")
    weaviate_object_ids: list[str] = Field(default_factory=list, description="VDB 139에 복사된 Weaviate 객체 ID 목록입니다.")
    status: str = Field(description="문서별 처리 상태입니다. committed, skipped_duplicate, no_chunks 등이 들어갑니다.")
    notes: list[str] = Field(default_factory=list)
    sql_tables: list["SqlTableMetadata"] = Field(
        default_factory=list,
        description="SQL 라우팅 문서의 사용자/LLM용 논리 테이블 메타데이터입니다.",
    )


class CommitResponse(BaseModel):
    mode: str = Field(default="commit", description="응답 모드입니다.")
    commit_enabled: bool = Field(description="현재 서비스에서 commit 쓰기가 활성화되어 있는지 여부입니다.")
    write_count: int = Field(description="이번 commit에서 수행한 DB/VDB 쓰기 개수의 개략값입니다.")
    target_vdb_id: int = Field(description="정식 등록 대상 VDB ID입니다.")
    target_collection: str = Field(description="정식 등록 대상 Weaviate 컬렉션명입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    documents: list[CommitDocumentResult] = Field(description="문서별 commit 결과입니다.")
    committed_count: int = Field(default=0, description="이번 요청에서 신규 등록된 문서 수입니다.")
    skipped_duplicate_count: int = Field(default=0, description="이미 등록되어 중복으로 건너뛴 문서 수입니다.")
    session_document_count: int | None = Field(default=None, description="commit 이후 같은 세션에 등록된 문서 수입니다.")
    file_only_ready: bool = Field(default=False, description="문서가 있고 no_chunks 실패가 없어 검색 준비가 된 상태인지 나타냅니다.")
    quota: "QuotaSnapshot | None" = Field(default=None, description="commit 시점의 쿼터 계산 결과입니다.")
    rollback_hint: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SessionDocument(BaseModel):
    document_id: int = Field(description="검색/삭제 대상이 되는 정식 GenOS document ID입니다.")
    file_name: str = Field(description="등록된 파일명입니다.")
    temp_document_id: int | None = Field(default=None, description="원본 임시 문서 ID입니다.")
    source_doc_key: str | None = Field(default=None, description="원본 추적과 중복 방지에 사용한 source document key입니다.")
    source_collection: str | None = Field(default=None, description="commit 당시 청크를 읽은 임시 collection입니다.")
    uploaded_at: str = Field(description="document row 생성 시각입니다.")
    expires_at: str | None = Field(default=None, description="세션 문서 TTL 만료 시각입니다.")
    file_size_bytes: int = Field(default=0, description="파일 크기 byte입니다.")
    chunk_count: int = Field(default=0, description="등록된 청크 수입니다.")
    is_expired: bool = Field(default=False, description="현재 시각 기준 TTL이 만료되었는지 여부입니다.")
    storage_route: Literal["vdb", "sql", "hybrid"] = Field(
        default="vdb",
        description=(
            "문서의 실제 검색 경로입니다. hybrid는 SQL 후보 시트와 잔여 VDB 시트를 "
            "함께 보존하며, 기존 문서는 vdb로 간주합니다."
        ),
    )
    route_reason: str = Field(default="", description="결정론적 라우팅 판정 근거입니다.")
    sql_tables: list["SqlTableMetadata"] = Field(default_factory=list)


class QuotaLimits(BaseModel):
    max_files: int = Field(description="세션당 허용되는 최대 등록 파일 수입니다.")
    max_per_request: int = Field(description="한 번의 /upload 요청에 허용되는 최대 파일 수입니다.")
    max_file_mb: int = Field(description="파일 1개당 허용되는 최대 크기(MB)입니다.")
    max_session_mb: int = Field(description="세션 전체에 허용되는 최대 파일 크기 합계(MB)입니다.")


class QuotaSnapshot(BaseModel):
    limits: QuotaLimits = Field(description="현재 서비스에 설정된 쿼터 한도입니다.")
    current_files: int = Field(description="현재 세션에 commit되어 활성인 파일 수입니다.")
    current_bytes: int = Field(description="현재 세션에 commit되어 활성인 파일 크기 합계(byte)입니다.")
    incoming_files: int = Field(default=0, description="이번 업로드/commit에서 추가 검토 중인 파일 수입니다.")
    incoming_bytes: int = Field(default=0, description="이번 업로드/commit에서 추가 검토 중인 파일 크기 합계(byte)입니다.")
    allowed: bool = Field(default=True, description="현재 요청 또는 세션 상태가 쿼터를 만족하는지 여부입니다.")
    violations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DocumentsResponse(BaseModel):
    target_vdb_id: int = Field(description="조회 대상 VDB ID입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    session_id: str = Field(description="실제로 사용된 세션 키입니다. chat_id가 있으면 chat_id입니다.")
    documents: list[SessionDocument] = Field(description="같은 세션에 commit되어 검색 가능한 문서 목록입니다.")
    errors: list[str] = Field(default_factory=list)


class QuotaCheckResponse(BaseModel):
    target_vdb_id: int = Field(description="쿼터 확인 대상 VDB ID입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    session_id: str = Field(description="실제로 사용된 세션 키입니다. chat_id가 있으면 chat_id입니다.")
    quota: QuotaSnapshot = Field(description="현재 세션의 쿼터 상태입니다.")
    errors: list[str] = Field(default_factory=list)


class UploadResponse(BaseModel):
    mode: str = Field(default="upload", description="응답 모드입니다.")
    target_vdb_id: int = Field(description="정식 등록 대상 VDB ID입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    session_id: str = Field(description="실제로 사용된 세션 키입니다. chat_id가 있으면 chat_id입니다.")
    temp_vdb_index_id: int | None = Field(default=None, description="생성된 임시 VDB index ID입니다.")
    temp_vdb_index: str | None = Field(default=None, description="전처리기가 청크를 저장한 임시 VDB index 이름입니다.")
    temp_documents: list[UploadedTempDocument] = Field(default_factory=list, description="/commit에 넘길 임시 문서 목록입니다.")
    commit: CommitResponse | None = Field(
        default=None,
        description=(
            "/upload 내부에서 이어서 수행한 정식 등록 결과입니다. 값이 있고 errors가 비어 있으며 "
            "file_only_ready=true이면 같은 세션에서 곧바로 /search가 가능합니다."
        ),
    )
    quota: QuotaSnapshot | None = Field(default=None, description="업로드 시점의 쿼터 계산 결과입니다.")
    blocked_uploads: list[BlockedUpload] = Field(
        default_factory=list,
        description="preprocessor-64 위임 전에 차단된 파일 목록입니다.",
    )
    errors: list[str] = Field(default_factory=list)


class DeleteDocumentResponse(BaseModel):
    target_vdb_id: int = Field(description="삭제 대상 VDB ID입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    session_id: str = Field(description="실제로 사용된 세션 키입니다. chat_id가 있으면 chat_id입니다.")
    document_id: int | None = Field(default=None, description="삭제 대상 GenOS document ID입니다.")
    temp_document_id: int | None = Field(default=None, description="삭제 대상의 원본 임시 문서 ID입니다.")
    status: str = Field(description="삭제 처리 상태입니다. rejected, deleted, not_found 등이 들어갑니다.")
    write_count: int = Field(default=0, description="삭제 과정에서 수행한 DB/VDB 쓰기 개수의 개략값입니다.")
    deleted_weaviate_object_ids: list[str] = Field(default_factory=list, description="삭제된 Weaviate 객체 ID 목록입니다.")
    rollback_hint: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class FileSource(BaseModel):
    document_id: int = Field(description="검색 결과 청크가 속한 GenOS document ID입니다.")
    file_name: str = Field(description="검색 결과 청크가 속한 파일명입니다.")
    chunk_id: str | None = Field(default=None, description="Weaviate 객체/chunk ID입니다.")
    i_page: int | None = Field(default=None, description="원본 문서 내 페이지 번호입니다.")
    i_chunk_on_doc: int | None = Field(default=None, description="원본 문서 내 청크 순번입니다.")
    distance: float | None = Field(default=None, description="벡터 검색 distance 값입니다. 낮을수록 질문과 가까운 결과입니다.")
    source_channel: str = Field(default="native_text", description="native text 또는 이미지 추출 provenance 채널입니다.")
    visual_model: str | None = Field(default=None, description="이미지 추출 청크를 생성한 모델입니다.")
    slide_number: int | None = Field(default=None, description="PPTX 원본 내 슬라이드 번호입니다.")
    section_title: str | None = Field(default=None, description="DOCX 원본 내 가장 가까운 섹션 제목입니다.")


class EmptyPageSource(BaseModel):
    """검색에서 근거 제외된 빈 페이지 자리표시 청크의 상태 메타데이터입니다."""

    document_id: int = Field(description="자리표시 청크가 속한 GenOS document ID입니다.")
    file_name: str = Field(description="자리표시 청크가 속한 파일명입니다.")
    chunk_id: str | None = Field(default=None, description="Weaviate 객체/chunk ID입니다.")
    i_page: int | None = Field(default=None, description="원본 문서 내 페이지 번호입니다.")
    status: str = Field(
        description=(
            "페이지 시각 콘텐츠 처리 상태입니다. visual_content_not_processed는 네이티브 텍스트가 없고 "
            "시각 채널 처리도 확인되지 않은 페이지, visual_content_processed는 같은 페이지가 VLM 시각 "
            "채널로 별도 처리된 경우입니다."
        )
    )


class SqlTableMetadata(BaseModel):
    logical_name: str = Field(description="세션 내부에서만 유효한 논리 테이블 이름입니다.")
    sheet_name: str = Field(description="원본 XLSX 시트 이름입니다.")
    row_count: int = Field(description="라우팅 시 관측한 시트 행 수입니다.")
    column_count: int = Field(description="라우팅 시 관측한 시트 열 수입니다.")


class FileSqlSource(SqlTableMetadata):
    document_id: int = Field(description="소유권 검증에 사용한 document ID입니다.")
    file_name: str = Field(description="사용자가 업로드한 원본 파일명입니다.")


class FileSqlColumn(BaseModel):
    query_name: str
    source_name: str


class FileSqlSchemaResponse(BaseModel):
    logical_name: str
    query_table: Literal["data"] = "data"
    columns: list[FileSqlColumn]
    llm_description: str


class FileSqlQueryResponse(BaseModel):
    logical_name: str
    columns: list[str]
    rows: list[list[str | int | float | None]]
    row_count: int


class SearchResponse(BaseModel):
    target_vdb_id: int = Field(description="검색 대상 VDB ID입니다.")
    workflow_id: int = Field(description="요청 workflow ID입니다.")
    app_session_id: str = Field(description="요청 app_session_id입니다.")
    session_id: str = Field(description="실제로 사용된 세션 키입니다. chat_id가 있으면 chat_id입니다.")
    question: str = Field(description="검색에 사용한 사용자 질문입니다.")
    document_count: int = Field(description="검색 후보로 제한된 세션 등록 문서 수입니다.")
    result_count: int = Field(description="VDB 검색으로 반환된 청크 수입니다.")
    file_context: str = Field(description="wf301 채팅 답변에 주입할 파일 기반 컨텍스트 문자열입니다.")
    file_sources: list[FileSource] = Field(description="file_context를 구성한 검색 출처 목록입니다.")
    sql_available: bool = Field(
        default=False,
        description="현재 세션에 조건부 SQL 라우팅 문서가 있는지 나타냅니다.",
    )
    sql_sources: list[FileSqlSource] = Field(
        default_factory=list,
        description="chat이 schema/query API에 전달할 세션 소유 논리 테이블 목록입니다.",
    )
    empty_page_sources: list[EmptyPageSource] = Field(
        default_factory=list,
        description=(
            "검색 상위 결과에 있었지만 근거에서 제외된 빈 페이지 자리표시 청크 목록입니다. "
            "chat은 이 목록으로 '해당 페이지의 시각 콘텐츠가 처리되지 않았음'을 명시할 수 있습니다."
        ),
    )
    errors: list[str] = Field(default_factory=list)


class PublicUploadedTempDocument(BaseModel):
    """User-visible metadata for a staged upload."""

    file_name: str


class PublicBlockedUpload(BaseModel):
    """User-visible upload rejection without internal diagnostics."""

    file_name: str
    route: Literal["blocked_oversized", "preprocess_failed"]
    message: str


class PublicSqlTableMetadata(BaseModel):
    """The SQL routing contract required by the chat consumer."""

    logical_name: str
    sheet_name: str
    row_count: int
    column_count: int


class PublicCommitDocumentResult(BaseModel):
    """A committed document with only user and query-routing metadata."""

    file_name: str
    chunk_count: int
    route: Literal["vdb", "vdb_large", "blocked_oversized", "sql"]
    status: str
    sql_tables: list[PublicSqlTableMetadata] = Field(default_factory=list)


class PublicCommitResponse(BaseModel):
    """Public commit result projected from the internal ledger response."""

    mode: str = "commit"
    documents: list[PublicCommitDocumentResult]
    committed_count: int = 0
    skipped_duplicate_count: int = 0
    file_only_ready: bool = False


class PublicUploadResponse(BaseModel):
    """Public upload result that excludes topology and ownership identifiers."""

    mode: str = "upload"
    temp_documents: list[PublicUploadedTempDocument] = Field(default_factory=list)
    commit: PublicCommitResponse | None = None
    blocked_uploads: list[PublicBlockedUpload] = Field(default_factory=list)


class PublicSessionDocument(BaseModel):
    """A user asset without ledger identifiers or storage topology."""

    file_name: str
    uploaded_at: str
    expires_at: str | None = None
    file_size_bytes: int = 0
    chunk_count: int = 0
    is_expired: bool = False
    storage_route: Literal["vdb", "sql", "hybrid"] = "vdb"
    route_reason: str = ""
    sql_tables: list[PublicSqlTableMetadata] = Field(default_factory=list)


class PublicDocumentsResponse(BaseModel):
    """Session assets projected without session and document identifiers."""

    documents: list[PublicSessionDocument]


class PublicFileSource(BaseModel):
    """User-facing provenance for one retrieved file passage."""

    file_name: str
    i_page: int | None = None
    source_channel: str = "native_text"
    visual_model: str | None = None
    slide_number: int | None = None
    section_title: str | None = None


class PublicEmptyPageSource(BaseModel):
    """User-facing state for an empty source page."""

    file_name: str
    i_page: int | None = None
    status: str


class PublicFileSqlSource(PublicSqlTableMetadata):
    """Session-scoped logical SQL source consumed by chat."""

    file_name: str


class PublicSearchResponse(BaseModel):
    """Search response projected for answer assembly and provenance display."""

    question: str
    document_count: int
    result_count: int
    file_context: str
    file_sources: list[PublicFileSource]
    sql_available: bool = False
    sql_sources: list[PublicFileSqlSource] = Field(default_factory=list)
    empty_page_sources: list[PublicEmptyPageSource] = Field(default_factory=list)
