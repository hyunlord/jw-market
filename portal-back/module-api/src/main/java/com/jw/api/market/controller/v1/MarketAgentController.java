package com.jw.api.market.controller.v1;

import com.jw.core.base.response.Response;
import com.jw.service.market.dto.v1.Market;
import com.jw.service.market.service.v1.MarketAgentService;
import com.jw.service.market.service.v1.MarketSseNotice;
import com.jw.service.market.service.v1.MarketSseTimingProbe;
import com.jw.service.rnd.dto.v1.Chat;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;
import reactor.core.Disposable;
import reactor.core.Disposables;

import java.io.IOException;
import java.time.Duration;
import java.util.function.Consumer;
import java.util.concurrent.atomic.AtomicBoolean;

@Tag(name = "MARKET Agent API - v1", description = "MARKET Agent 구성 API v1")
@RestController("MarketAgentControllerV1")
@RequestMapping("/api/v1/market/chat")
public class MarketAgentController {

    private static final Logger logger = LoggerFactory.getLogger(MarketAgentController.class);

    /**
     * 답변 하나가 스트리밍될 수 있는 시간. 이 값이 경로 상한 중 가장 낮아 실제 절단을 만든다.
     *
     * <p>120초였다. 그 예산은 소스 두세 개만 조회하던 시절의 것이고, 전 소스 fan-out 을 되살린
     * 뒤 실측한 30턴에서 15턴 중 12턴이 120초를 넘겼다(최대 214초). 즉 답이 완성돼 있어도
     * 사용자는 절단 고지만 보게 된다. 관측 최대치를 덮고도 여유가 남는 240초로 올린다.
     */
    private static final long CHAT_STREAM_TIMEOUT_MILLIS = Duration.ofSeconds(510).toMillis();

    /**
     * 상한은 설정으로 주입할 수 있다. 기본값은 위 상수와 같아 설정을 주지 않아도 동작이 정의된다.
     *
     * <p>외부 LB 600초에서 15%를 남긴 공통 예산 510초로 제한한다.
     */
    private static final long MAX_CHAT_STREAM_TIMEOUT_MILLIS = 510_000L;

    /** Container timeout 전에 사용자 고지를 쓸 수 있도록 남기는 전송 여유. */
    private static final long TIMEOUT_NOTICE_LEAD_MILLIS = 1_000L;

    private final MarketAgentService marketAgentService;
    private final MarketSseDeadlineScheduler deadlineScheduler;

    @Value("${portal.bff.sse.timing.enabled:false}")
    private boolean sseTimingEnabled;

    @Value("${portal.bff.sse.timeout.millis:${MARKET_SSE_TIMEOUT_MS:510000}}")
    private String configuredChatStreamTimeoutMillis = Long.toString(CHAT_STREAM_TIMEOUT_MILLIS);

    private long chatStreamTimeoutMillis = CHAT_STREAM_TIMEOUT_MILLIS;

    @Autowired
    public MarketAgentController(
        @Qualifier("MarketAgentServiceV1") MarketAgentService marketAgentService
    ) {
        this(marketAgentService, MarketSseDeadlineScheduler.reactorParallel());
    }

    MarketAgentController(
        MarketAgentService marketAgentService,
        MarketSseDeadlineScheduler deadlineScheduler
    ) {
        this.marketAgentService = marketAgentService;
        this.deadlineScheduler = deadlineScheduler;
    }

    @PostConstruct
    void configureChatStreamTimeout() {
        chatStreamTimeoutMillis = resolveChatStreamTimeout(
            configuredChatStreamTimeoutMillis,
            logger::warn);
        logger.info(
            "event=market_sse_timeout_config effective_ms={} notice_lead_ms={} "
                + "application_deadline_ms={} source=portal.bff.sse.timeout.millis",
            chatStreamTimeoutMillis,
            TIMEOUT_NOTICE_LEAD_MILLIS,
            timeoutNoticeDelayMillis(chatStreamTimeoutMillis));
    }

    static long resolveChatStreamTimeout(String configured, Consumer<String> warningSink) {
        final long parsed;
        try {
            parsed = Long.parseLong(configured);
        } catch (RuntimeException error) {
            warningSink.accept(
                "event=market_sse_timeout_config action=fallback reason=non_numeric effective_ms=510000");
            return CHAT_STREAM_TIMEOUT_MILLIS;
        }
        if (parsed <= 0L) {
            warningSink.accept(
                "event=market_sse_timeout_config action=fallback reason=non_positive effective_ms=510000");
            return CHAT_STREAM_TIMEOUT_MILLIS;
        }
        if (parsed > MAX_CHAT_STREAM_TIMEOUT_MILLIS) {
            warningSink.accept(String.format(
                java.util.Locale.ROOT,
                "event=market_sse_timeout_config action=clamped configured_ms=%d effective_ms=%d",
                parsed,
                MAX_CHAT_STREAM_TIMEOUT_MILLIS));
            return MAX_CHAT_STREAM_TIMEOUT_MILLIS;
        }
        return parsed;
    }

    static long timeoutNoticeDelayMillis(long streamTimeoutMillis) {
        return Math.max(1L, streamTimeoutMillis - TIMEOUT_NOTICE_LEAD_MILLIS);
    }

    @Operation(
        summary = "MARKET Chat 상태 확인",
        description = "MARKET AI endpoint의 배포·승인 상태 조회 (health check)"
    )
    @PostMapping("/info")
    public ResponseEntity<Response> chatInfo(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Info chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppInfo(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 질문",
        description = "MARKET AI CHAT 질문"
    )
    @PostMapping("/query")
    public ResponseEntity<Response> chatQuery(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppQuery(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 상세 조회",
        description = "답변 근거 또는 조회 상세 항목을 사용자 소유권 범위에서 조회"
    )
    @GetMapping("/detail/{conversationId}/{responseId}")
    public ResponseEntity<Response> chatDetail(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @PathVariable String conversationId,
        @PathVariable String responseId,
        @RequestParam("item_key") String itemKey
    ) {
        return ResponseEntity.ok(
            marketAgentService.getChatDetail(
                accessToken,
                conversationId,
                responseId,
                itemKey
            )
        );
    }

    @Operation(
        summary = "MARKET Chat 질문 (SSE 스트리밍)",
        description = "MARKET AI CHAT 질문 - text/event-stream 실시간 스트리밍 응답"
    )
    @PostMapping(value = "/query/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter chatQueryStream(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Chat.Stream marketRequest
    ) {
        MarketSseTimingProbe timingProbe = MarketSseTimingProbe.start(sseTimingEnabled);
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.start(
            marketRequest.getConversationId());
        SseEmitter emitter = new SseEmitter(chatStreamTimeoutMillis);
        Disposable.Swap subscription = Disposables.swap();
        Disposable.Swap deadline = Disposables.swap();
        AtomicBoolean terminated = new AtomicBoolean();

        emitter.onCompletion(() -> {
            deadline.dispose();
            subscription.dispose();
        });
        emitter.onTimeout(() -> {
            deadline.dispose();
            completeAtDeadline(emitter, subscription, terminated, telemetry);
        });
        emitter.onError(error -> {
            deadline.dispose();
            subscription.dispose();
            if (terminated.compareAndSet(false, true)) {
                telemetry.finish("downstream_error", false);
            }
        });

        var stream = sseTimingEnabled
            ? marketAgentService.getChatAppQueryStream(accessToken, marketRequest, timingProbe)
            : marketAgentService.getChatAppQueryStream(accessToken, marketRequest);

        deadline.replace(deadlineScheduler.schedule(
            () -> completeAtDeadline(emitter, subscription, terminated, telemetry),
            timeoutNoticeDelayMillis(chatStreamTimeoutMillis)));

        subscription.replace(stream
            .subscribe(
                event -> sendEvent(
                    emitter,
                    subscription,
                    deadline,
                    terminated,
                    timingProbe,
                    telemetry,
                    event),
                error -> {
                    deadline.dispose();
                    if (terminated.compareAndSet(false, true)) {
                        telemetry.finish("upstream_error", false);
                        emitter.completeWithError(error);
                    }
                },
                () -> {
                    deadline.dispose();
                    if (terminated.compareAndSet(false, true)) {
                        telemetry.finish("normal_complete", false);
                        emitter.complete();
                    }
                }
            ));

        return emitter;
    }

    private static void completeAtDeadline(
        SseEmitter emitter,
        Disposable subscription,
        AtomicBoolean terminated,
        MarketSseStreamTelemetry telemetry
    ) {
        subscription.dispose();
        if (!terminated.compareAndSet(false, true)) {
            return;
        }
        if (telemetry.frameLimitSeen()) {
            telemetry.finish("frame_limit", true);
            emitter.complete();
            return;
        }
        completeWithTruncationNotice(emitter, telemetry);
    }

    /**
     * 상한 도달로 스트림을 마감할 때, 왜 잘렸는지를 사용자에게 남기고 마감한다.
     *
     * <p>이 자리에서 그냥 complete() 만 하면 HTTP 200 으로 정상 종료돼 사용자는 사유를 알 수 없고,
     * 포털은 본문이 비었다고 판단해 질문 말풍선까지 지운다. 고지를 본문 채널로 실어야 그 두 가지가
     * 모두 해소된다.
     *
     * <p>고지 전송이 실패하더라도 스트림 마감은 반드시 수행한다. 고지가 답변을 죽여서는 안 된다.
     * 다만 그 실패를 삼키지 않고 로그로 남긴다.
     */
    static void completeWithTruncationNotice(
        SseEmitter emitter,
        MarketSseStreamTelemetry telemetry
    ) {
        boolean hadBody = telemetry.bodySent();
        boolean noticeSent = false;
        try {
            for (ServerSentEvent<String> notice : MarketSseNotice.streamTimeoutEvents(hadBody)) {
                emitter.send(toBuilder(notice));
                telemetry.onNoticeSent(notice);
            }
            noticeSent = true;
        } catch (Exception noticeFailure) {
            logger.warn(
                "event=market_sse_notice_delivery outcome=timeout notice_sent=false error_type={}",
                noticeFailure.getClass().getSimpleName());
        } finally {
            telemetry.finish("timeout", noticeSent);
            emitter.complete();
        }
    }

    private static SseEmitter.SseEventBuilder toBuilder(ServerSentEvent<String> event) {
        SseEmitter.SseEventBuilder builder = SseEmitter.event();
        if (event.id() != null) {
            builder.id(event.id());
        }
        if (event.event() != null) {
            builder.name(event.event());
        }
        if (event.retry() != null) {
            builder.reconnectTime(event.retry().toMillis());
        }
        if (event.comment() != null) {
            builder.comment(event.comment());
        }
        if (event.data() != null) {
            builder.data(event.data());
        }
        return builder;
    }

    private static void sendEvent(
        SseEmitter emitter,
        Disposable subscription,
        Disposable deadline,
        AtomicBoolean terminated,
        MarketSseTimingProbe timingProbe,
        MarketSseStreamTelemetry telemetry,
        ServerSentEvent<String> event
    ) {
        SseEmitter.SseEventBuilder builder = toBuilder(event);

        try {
            emitter.send(builder);
            timingProbe.onEmitterSent(event);
            telemetry.onSent(event);
        } catch (IOException error) {
            deadline.dispose();
            subscription.dispose();
            if (terminated.compareAndSet(false, true)) {
                telemetry.finish("downstream_error", false);
                emitter.completeWithError(error);
            }
        }
    }

    @Operation(
        summary = "MARKET Chat 세션 목록 조회",
        description = "MARKET AI CHAT 세션 목록 조회"
    )
    @PostMapping("/session")
    public ResponseEntity<Response> chatSession(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppSession(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 고정 세션 목록 조회",
        description = "MARKET AI CHAT Pinned 세션 목록 조회"
    )
    @PostMapping("/session/pinned")
    public ResponseEntity<Response> chatSessionPinned(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Pinned chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppSessionPinned(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 계획 취소",
        description = "MARKET AI CHAT Cancel"
    )
    @PostMapping("/query/cancel")
    public ResponseEntity<Response> chatQueryCancel(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Cancel chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppQueryPlanCancel(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 계획 수정",
        description = "MARKET AI CHAT Reject"
    )
    @PostMapping("/query/reject")
    public ResponseEntity<Response> chatQueryReject(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Reject chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppQueryPlanReject(accessToken, chatAppRequest));
    }

    @Operation(
        summary = "MARKET Chat 계획 실행",
        description = "MARKET AI CHAT Proceed"
    )
    @PostMapping("/query/proceed")
    public ResponseEntity<Response> chatQueryProceed(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Query.Plan.Proceed chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppQueryPlanProceed(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 중단",
        description = "MARKET AI CHAT Stop"
    )
    @PostMapping("/abort")
    public ResponseEntity<Response> chatAbort(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Chat.App.Request.Abort chatAppRequest
    ) { return ResponseEntity.ok(marketAgentService.getChatAppAbort(accessToken, chatAppRequest)); }

    @Operation(
        summary = "MARKET Chat 문서 상태 확인",
        description = "MARKET AI CHAT 문서 Health"
    )
    @GetMapping(value = "/document/health")
    public ResponseEntity<Response> chatDocumentsHealth(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(marketAgentService.getDocumentsHealth(accessToken)); }

    @Operation(
        summary = "MARKET Chat 문서 목록 조회",
        description = "MARKET AI CHAT 문서 목록 조회"
    )
    @PostMapping(value = "/document")
    public ResponseEntity<Response> chatDocument(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Document marketRequest
    ) { return ResponseEntity.ok(marketAgentService.getDocuments(accessToken, marketRequest)); }

    @Operation(
        summary = "MARKET Chat 문서 삭제",
        description = "MARKET AI CHAT 문서 삭제"
    )
    @PutMapping(value = "/document/delete")
    public ResponseEntity<Response> chatDocumentDelete(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Document.Delete marketRequest
    ) { return ResponseEntity.ok(marketAgentService.delDocuments(accessToken, marketRequest)); }

    @Operation(
        summary = "MARKET Chat 문서 업로드",
        description = "MARKET AI CHAT 문서 Upload"
    )
    @PostMapping(value = "/document/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<Response> chatDocumentUpload(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Market.Request.Document.Upload marketRequest
    ) { return ResponseEntity.ok(marketAgentService.setDocumentsUpload(accessToken, marketRequest)); }

    @Operation(
        summary = "MARKET Chat 문서 업로드 상태 조회",
        description = "accepted 문서 업로드의 처리 상태 조회"
    )
    @GetMapping(value = "/document/upload/status")
    public ResponseEntity<Response> chatDocumentUploadStatus(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @ModelAttribute Market.Request.Document.UploadStatus marketRequest
    ) {
        return ResponseEntity.ok(
            marketAgentService.getDocumentsUploadStatus(accessToken, marketRequest)
        );
    }

}
