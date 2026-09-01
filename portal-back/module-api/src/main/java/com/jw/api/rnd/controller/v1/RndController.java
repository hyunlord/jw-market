package com.jw.api.rnd.controller.v1;

import com.jw.core.base.entity.UserAccess;
import com.jw.core.base.response.Response;
import com.jw.core.config.annotation.AccessUser;
import com.jw.service.user.dto.v1.Admin;
import com.jw.service.rnd.dto.v1.Chat;
import com.jw.service.rnd.service.v1.RndService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.Flux;

@Tag(name = "R&D Agent API - v1", description = "R&D Agent 구성 API v1")
@RestController("RndControllerV1")
@RequestMapping("/api/v1/rnd")
public class RndController {

    private final RndService rndService;

    public RndController(
        @Qualifier("RndServiceV1") RndService rndService
    ) {
        this.rndService = rndService;
    }

    @Operation(
        summary = "Admin 사용자 Token 조회",
        description = "Admin 사용자 Token 조회"
    )
    @GetMapping("/admin/credit")
    public ResponseEntity<Response> adminCredit(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(rndService.getChatAdminCredit(accessToken)); }

    @Operation(
        summary = "Admin 전체 Token, Cost 요약 조회",
        description = "시스템 전체 token + cost 요약 (메인 통계)"
    )
    @GetMapping("/admin/credit/costs/summary")
    public ResponseEntity<Response> adminCreditCostsSummary(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(rndService.getChatAdminCreditCostsSummary(accessToken)); }

    @Operation(
        summary = "Admin cost 누적 조회",
        description = "cost 누적 (token 정보 없이)"
    )
    @GetMapping("/admin/credit/costs")
    public ResponseEntity<Response> adminCreditCosts(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(rndService.getChatAdminCreditCosts(accessToken)); }

    @Operation(
        summary = "Admin Cost 서비스별 상세 조회",
        description = "Cost 분해 (모델/serving별 상세)"
    )
    @GetMapping("/admin/credit/costs/breakdown")
    public ResponseEntity<Response> adminCreditCostsBreakdown(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(rndService.getChatAdminCreditCostsBreakdown(accessToken)); }

    @Operation(
        summary = "Admin Cost 분포 조회",
        description = "Cost 분포 (source_type별 비율)"
    )
    @GetMapping("/admin/credit/costs/distribution")
    public ResponseEntity<Response> adminCreditCostsDistribution(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(rndService.getChatAdminCreditCostsDistribution(accessToken)); }

    @Operation(
        summary = "Admin Cost 시계열 트렌드 조회",
        description = "Cost 시계열 트렌드 (일별)"
    )
    @GetMapping("/admin/credit/costs/trend")
    public ResponseEntity<Response> adminCreditCostsTrend(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(rndService.getChatAdminCreditCostsTrend(accessToken)); }

    @Operation(
        summary = "Admin Credit 내역 조회",
        description = "Credit 지급/회수 raw history"
    )
    @PostMapping("/admin/credit/costs/history")
    public ResponseEntity<Response> adminCreditCostsHistory(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Admin.Request.History adminCreditRequest
    ) { return ResponseEntity.ok(rndService.getChatAdminCreditHistory(accessToken, adminCreditRequest)); }

    @Operation(
        summary = "Admin 출처 문서 Base64 조회",
        description = "Vectordb Download base64"
    )
    @PostMapping("/admin/vectordb/download")
    public ResponseEntity<Response> adminVectordbDownload(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Document chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAdminVectordbDownload(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 상태 확인",
        description = "R&D AI endpoint의 배포·승인 상태 조회 (health check)"
    )
    @PostMapping("/chat/info")
    public ResponseEntity<Response> chatInfo(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Info chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppInfo(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 질문",
        description = "R&D AI CHAT 질문"
    )
    @PostMapping("/chat/query")
    public ResponseEntity<Response> chatQuery(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppQuery(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 질문 (SSE 스트리밍)",
        description = "R&D AI CHAT 질문 - text/event-stream 실시간 스트리밍 응답"
    )
    @PostMapping(value = "/chat/query/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> chatQueryStream(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query chatAppRequest
    ) { return rndService.getChatAppQueryStream(accessToken, chatAppRequest); }

    @Operation(
        summary = "R&D 보고서 다운로드",
        description = "R&D AI CHAT Report Download"
    )
    @PostMapping("/chat/report")
    public ResponseEntity<Response> chatQueryReport(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Report chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppQueryReport(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 계획 취소",
        description = "R&D AI CHAT Cancel"
    )
    @PostMapping("/chat/query/cancel")
    public ResponseEntity<Response> chatQueryCancel(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Cancel chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppQueryPlanCancel(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 계획 수정",
        description = "R&D AI CHAT Reject"
    )
    @PostMapping("/chat/query/reject")
    public ResponseEntity<Response> chatQueryReject(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Reject chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppQueryPlanReject(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 계획 수정 (SSE 스트리밍)",
        description = "R&D AI CHAT 계획 수정 - text/event-stream 실시간 스트리밍 응답"
    )
    @PostMapping(value = "/chat/query/reject/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> chatQueryRejectStream(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Reject chatAppRequest
    ) { return rndService.getChatAppQueryPlanRejectStream(accessToken, chatAppRequest); }

    @Operation(
        summary = "R&D Chat 계획 실행",
        description = "R&D AI CHAT Proceed"
    )
    @PostMapping("/chat/query/proceed")
    public ResponseEntity<Response> chatQueryProceed(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Proceed chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppQueryPlanProceed(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 계획 실행 (SSE 스트리밍)",
        description = "R&D AI CHAT Proceed - text/event-stream 실시간 스트리밍 응답"
    )
    @PostMapping(value = "/chat/query/proceed/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> chatQueryProceedStream(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Proceed chatAppRequest
    ) { return rndService.getChatAppQueryPlanProceedStream(accessToken, chatAppRequest); }

    @Operation(
        summary = "R&D Chat 중단",
        description = "R&D AI CHAT Stop"
    )
    @PostMapping("/chat/abort")
    public ResponseEntity<Response> chatAbort(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Abort chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppAbort(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D 첨부 문서 삭제",
        description = "R&D 첨부 문서 삭제"
    )
    @PutMapping("/chat/document/delete")
    public ResponseEntity<Response> chatDocument(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestParam Long tempDocumentId
    ) { return ResponseEntity.ok(rndService.delChatAppTempDocument(accessToken, tempDocumentId)); }

    @Operation(
        summary = "R&D Chat 사용자 서비스 목록 조회",
        description = "R&D AI CHAT User Bot Service 목록 조회"
    )
    @PostMapping("/chat/list")
    public ResponseEntity<Response> chatList(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.ChatList chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppList(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 세션 목록 조회",
        description = "R&D AI CHAT 세션 목록 조회"
    )
    @PostMapping("/chat/session")
    public ResponseEntity<Response> chatSession(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppSession(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 고정 세션 목록 조회",
        description = "R&D AI CHAT Pinned 세션 목록 조회"
    )
    @PostMapping("/chat/session/pinned")
    public ResponseEntity<Response> chatSessionPinned(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Pinned chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppSessionPinned(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 세션 제목 수정",
        description = "R&D AI CHAT 세션 Title 수정"
    )
    @PutMapping("/chat/session/rename")
    public ResponseEntity<Response> chatSessionRename(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Rename chatAppRequest
    ) { return ResponseEntity.ok(rndService.updChatAppSessionRename(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 고정 세션 삭제",
        description = "R&D AI CHAT 세션 삭제"
    )
    @PutMapping("/chat/session/delete")
    public ResponseEntity<Response> chatSessionDelete(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Delete chatAppRequest
    ) { return ResponseEntity.ok(rndService.delChatAppSessionDelete(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 세션 고정 등록",
        description = "R&D AI CHAT 세션 고정 등록"
    )
    @PutMapping("/chat/session/pin")
    public ResponseEntity<Response> chatSessionPin(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Pinned.Pin chatAppRequest
    ) { return ResponseEntity.ok(rndService.setChatAppSessionPin(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 고정 세션 해제",
        description = "R&D AI CHAT 세션 고정 해제"
    )
    @PutMapping("/chat/session/unpin")
    public ResponseEntity<Response> chatSessionUnpin(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Pinned.Unpin chatAppRequest
    ) { return ResponseEntity.ok(rndService.updChatAppSessionUnpin(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 세션 조회",
        description = "R&D AI CHAT 세션 조회"
    )
    @PostMapping("/chat/search/session")
    public ResponseEntity<Response> chatSearchSession(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Search.Session chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppSearchSession(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 내용 통합 조회",
        description = "R&D AI CHAT 내용 통합 조회"
    )
    @PostMapping("/chat/search/message")
    public ResponseEntity<Response> chatSearchMessage(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Search.Message chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppSearchMessage(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 이전 내역 조회",
        description = "R&D AI CHAT 세션의 이전 Q&A + 검색 통합 조회"
    )
    @PostMapping("/chat/log")
    public ResponseEntity<Response> chatLog(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Log chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppLog(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 출처 문서 Base64 조회",
        description = "R&D AI CHAT 문서 base64 encoded file 조회"
    )
    @PostMapping("/chat/document")
    public ResponseEntity<Response> chatDocument(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Document chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppDocument(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 출처 문서 뷰어 URL 조회",
        description = "R&D AI CHAT presigned URL 발급 — 참고 문서 뷰어용"
    )
    @PostMapping("/chat/document/media")
    public ResponseEntity<Response> chatDocumentMedia(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Document chatAppRequest
    ) { return ResponseEntity.ok(rndService.getChatAppDocumentMedia(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D Chat 문서 업로드",
        description = "R&D AI CHAT 문서 Upload"
    )
    @PostMapping(value = "/chat/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<Response> chatUpload(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Chat.App.Request.Upload chatAppRequest
    ) { return ResponseEntity.ok(rndService.setChatAppUpload(accessToken, chatAppRequest)); }

    @Operation(
        summary = "R&D VectorDB 목록 조회",
        description = "R&D 이름(name) 등으로 벡터 DB 검색 → vdb_id 확보"
    )
    @PostMapping("/admin/vector/list")
    public ResponseEntity<Response> adminVector(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Admin.Request.VectorDB adminRequest
    ) { return ResponseEntity.ok(rndService.getAdminDataVectorDBList(accessToken, adminRequest)); }

    @Operation(
        summary = "R&D VectorDB 문서 목록 조회",
        description = "R&D vdb_id로 해당 VDB의 문서 목록 조회 → 각 문서의 file_name 사용"
    )
    @PostMapping("/admin/vector/document/list")
    public ResponseEntity<Response> adminVectorDocument(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Admin.Request.VectorDB.Document adminRequest
    ) { return ResponseEntity.ok(rndService.getAdminDataVectorDBDocumentList(accessToken, adminRequest)); }

    @Operation(
        summary = "R&D VectorDB 사내 문서 목록 조회",
        description = "R&D VectorDB 사내 문서 고정 ID 목록 조회"
    )
    @PostMapping("/admin/vector/document/list/in")
    public ResponseEntity<Response> adminVectorDocumentIn(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Admin.Request.VectorDB.Document adminRequest
    ) { return ResponseEntity.ok(rndService.getAdminDataVectorDBIn(accessToken, adminRequest)); }

    @Operation(
        summary = "R&D VectorDB 논문 문서 목록 조회",
        description = "R&D VectorDB 논문 문서 고정 ID 목록 조회"
    )
    @PostMapping("/admin/vector/document/list/thesis")
    public ResponseEntity<Response> adminVectorDocumentThesis(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Admin.Request.VectorDB.Document adminRequest
    ) { return ResponseEntity.ok(rndService.getAdminDataVectorDBThesis(accessToken, adminRequest)); }
}
