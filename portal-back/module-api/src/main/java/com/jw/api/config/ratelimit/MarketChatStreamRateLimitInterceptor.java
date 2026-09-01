package com.jw.api.config.ratelimit;

import com.jw.core.auth.jwt.JwtTokenProvider;
import com.jw.core.base.BaseEnum;
import com.jw.core.base.response.ExceptionResponse;
import com.jw.core.ratelimit.SlidingWindowRateLimiter;
import com.jw.core.util.TextCore;
import com.jw.core.util.mapper.ObjectMapper;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.servlet.HandlerInterceptor;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * MARKET Chat SSE 스트리밍 엔드포인트({@code POST /api/*&#47;market/chat/query/stream})
 * 계정별 요청 빈도 제한.
 *
 * <p><b>왜 인터셉터인가</b> — Spring Security 필터체인 밖, DispatcherServlet 안에 위치한다.
 * 인증·인가 로직을 전혀 건드리지 않으면서(이 회차 범위 밖) 해당 경로만 정확히 겨냥할 수 있다.
 * 429 응답은 {@code preHandle} 에서 직접 기록하므로
 * {@code GlobalExceptionHandler} 의 catch-all(Exception → 500)에 삼켜지지 않는다.
 *
 * <p><b>계정 키</b> — {@code Authorization-Access-Token} 헤더 payload 의 {@code uid}.
 * {@code SecurityContextHolder} 를 쓰지 않는다: 이 경로는 {@code permitAll} 이라
 * {@code Authorization} 헤더를 생략해도 통과하고, 그때 principal 이 비어
 * <b>모든 익명 호출자가 하나의 전역 버킷으로 합류</b>해 버린다. 반면
 * {@code Authorization-Access-Token} 은 컨트롤러가 필수로 요구하는 값이고,
 * {@code MarketAgentService} 가 이미 이 값의 {@code uid} 를 {@code X-Portal-User-Id} 로
 * 하류에 넘기고 있어 "계정" 축이 하류와 정확히 일치한다.
 *
 * <p><b>키를 못 얻는 요청</b>(헤더 없음/JWT 아님/uid 없음)은 통과시키지도, 전역 버킷에
 * 합치지도 않고 <b>클라이언트 IP 별 별도 버킷</b>으로 계량한다. 상세는 {@link #resolveKey}.
 *
 * <p><b>★ in-memory 한계</b> — 카운터는 프로세스 로컬이다. 실효 한도 = 설정값 × replica 수.
 * test2 는 replica 1 이라 정확하지만 <b>운영 {@code portal-back} 은 replica 2 이므로
 * 한도가 2배로 벌어진다.</b> 운영 이관은 공유 저장소 설계를 선행해야 하는 별도 회차다.
 * 자세한 내용은 {@link SlidingWindowRateLimiter} 클래스 주석 참조.
 */
@Component
public class MarketChatStreamRateLimitInterceptor implements HandlerInterceptor {

    private static final Logger logger = LoggerFactory.getLogger(MarketChatStreamRateLimitInterceptor.class);

    private static final String ACCESS_TOKEN_HEADER = "Authorization-Access-Token";
    private static final String METRIC_BLOCKED = "portal.ratelimit.blocked";
    private static final String ENDPOINT_TAG = "market_chat_stream";
    private static final String USER_MESSAGE = "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.";

    private final boolean enabled;
    private final SlidingWindowRateLimiter limiter;
    private final JwtTokenProvider tokenProvider;
    private final ObjectProvider<MeterRegistry> meterRegistryProvider;

    public MarketChatStreamRateLimitInterceptor(
        @Value("${portal.ratelimit.market-chat-stream.enabled:true}") boolean enabled,
        @Value("${portal.ratelimit.market-chat-stream.requests-per-minute:10}") int requestsPerMinute,
        @Value("${portal.ratelimit.market-chat-stream.max-tracked-keys:20000}") int maxTrackedKeys,
        JwtTokenProvider tokenProvider,
        ObjectProvider<MeterRegistry> meterRegistryProvider
    ) {
        this.enabled = enabled;
        this.limiter = new SlidingWindowRateLimiter(requestsPerMinute, maxTrackedKeys);
        this.tokenProvider = tokenProvider;
        this.meterRegistryProvider = meterRegistryProvider;

        logger.info(
            "market chat stream rate limit: enabled={} limitPerMinute={} maxTrackedKeys={} "
                + "(in-memory, per-replica: effective limit = limitPerMinute x replicas)",
            enabled, requestsPerMinute, maxTrackedKeys
        );
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler)
        throws Exception {

        if (!enabled) {
            return true;
        }

        // ★ 이 엔드포인트는 Flux(SSE) 를 반환하는 ★비동기 핸들러다.
        // Spring MVC 는 비동기 처리가 끝나면 같은 요청을 DispatcherType.ASYNC 로 ★재디스패치하고,
        // 그때 preHandle 이 ★한 번 더 호출된다. 재디스패치를 함께 세면 두 가지가 깨진다:
        //   (1) 스트리밍 요청 1건이 카운터를 ★2개 소모한다 (실효 한도가 절반이 됨)
        //   (2) 재디스패치가 차단되면 ★이미 커밋된 응답에 429 를 쓰려다 진행 중이던
        //       SSE 스트림을 ★잘라먹는다 (curl: transfer closed with outstanding read data)
        // 판정은 ★최초 디스패치에서 한 번만 한다.
        if (!DispatcherType.REQUEST.equals(request.getDispatcherType())) {
            return true;
        }

        String key = this.resolveKey(request);
        SlidingWindowRateLimiter.Decision decision = limiter.check(key, System.currentTimeMillis());

        if (decision.allowed()) {
            // 한도 내 요청은 완전히 불변으로 통과시킨다 — 상태코드·헤더·본문 무변경.
            return true;
        }

        this.writeTooManyRequests(request, response, key, decision);
        return false;
    }

    /**
     * 계정 키를 결정한다.
     *
     * <ul>
     *   <li>{@code uid:<uid>} — 정상 경로. 토큰 payload 에서 uid 를 얻은 경우.</li>
     *   <li>{@code anon:<ip>} — 헤더 없음 / JWT 아님 / uid 없음. 이 요청들은 어차피
     *       하류(chat)에 도달하지 못하고 400·500 으로 끝나지만, 무제한 방치하면 BFF 자체를
     *       소모시킬 수 있으므로 계량한다. <b>전역 단일 버킷으로 합치지 않고</b>
     *       클라이언트 IP 로 분리해, 한 명이 익명 부류 전체를 굶기지 못하게 한다.</li>
     * </ul>
     *
     * <p>주의: 프록시(Istio/GKE LB) 뒤에서 {@code X-Forwarded-For} 가 없으면 remoteAddr 이
     * 사이드카 주소로 수렴해 익명 부류가 사실상 한 버킷이 될 수 있다. 익명 부류는 이미
     * 실패하는 트래픽이라 수용 가능한 열화로 본다(정상 계정 격리에는 영향 없음).
     */
    private String resolveKey(HttpServletRequest request) {
        String token = request.getHeader(ACCESS_TOKEN_HEADER);
        if (StringUtils.hasText(token)) {
            try {
                Long uid = tokenProvider.getParseClaims(token).getUid();
                if (uid != null) {
                    return "uid:" + uid;
                }
            } catch (Exception ignored) {
                // 토큰이 JWT 형식이 아니거나 payload 파싱 실패 → 익명으로 강등.
                // 여기서 예외를 올리면 기존 500 거동을 바꾸게 되므로 삼키고 fall-through 한다.
            }
        }
        return "anon:" + this.clientIp(request);
    }

    private String clientIp(HttpServletRequest request) {
        String forwarded = request.getHeader("X-Forwarded-For");
        if (StringUtils.hasText(forwarded)) {
            int comma = forwarded.indexOf(',');
            String first = (comma == -1 ? forwarded : forwarded.substring(0, comma)).trim();
            if (StringUtils.hasText(first)) {
                return first;
            }
        }
        String remote = request.getRemoteAddr();
        return StringUtils.hasText(remote) ? remote : "unknown";
    }

    private void writeTooManyRequests(
        HttpServletRequest request,
        HttpServletResponse response,
        String key,
        SlidingWindowRateLimiter.Decision decision
    ) throws Exception {

        String traceId = TextCore.uuidV1().substring(0, 10);
        String maskedKey = this.maskKey(key);

        // 안전판: 응답이 이미 커밋됐다면 상태·헤더·본문을 쓸 수 없다.
        // (위 DispatcherType 가드로 도달하지 않아야 정상이지만, 스트림을 잘라먹는 것보다
        //  차단을 포기하는 편이 낫다 — 사건은 로그·메트릭에 그대로 남긴다.)
        boolean writable = !response.isCommitted();

        // ★ 차단 사건을 조용히 삼키지 않는다 — 로그와 메트릭 양쪽에 남긴다.
        logger.warn(
            "market chat stream rate limit BLOCKED: keyHash={} keyKind={} limitPerMinute={} "
                + "retryAfterSeconds={} traceId={} path={}",
            maskedKey, this.keyKind(key), decision.limit(), decision.retryAfterSeconds(),
            traceId, request.getRequestURI()
        );

        MeterRegistry meterRegistry = meterRegistryProvider.getIfAvailable();
        if (meterRegistry != null) {
            meterRegistry.counter(
                METRIC_BLOCKED,
                "endpoint", ENDPOINT_TAG,
                "key_kind", this.keyKind(key)
            ).increment();
        }

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("retryAfterSeconds", decision.retryAfterSeconds());
        detail.put("limitPerMinute", decision.limit());
        detail.put("traceId", traceId);
        detail.put("path", request.getRequestURI());

        ExceptionResponse body = new ExceptionResponse(
            null,
            BaseEnum.Response.Status.FAIL,
            USER_MESSAGE,
            429,
            detail.toString()
        );

        if (!writable) {
            logger.warn("market chat stream rate limit: response already committed, "
                + "cannot emit 429 (traceId={}) — leaving the in-flight response intact", traceId);
            return;
        }

        response.setStatus(429);
        response.setHeader("Retry-After", String.valueOf(decision.retryAfterSeconds()));
        response.setHeader("X-RateLimit-Limit", String.valueOf(decision.limit()));
        response.setHeader("X-RateLimit-Remaining", "0");
        response.setContentType("application/json;charset=UTF-8");
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.getWriter().write(ObjectMapper.writeValueAsString(body));
        response.getWriter().flush();
    }

    private String keyKind(String key) {
        return key.startsWith("uid:") ? "uid" : "anon";
    }

    /** 계정 식별자를 로그에 평문으로 남기지 않는다 — SHA-256 앞 12 hex 만. */
    private String maskKey(String key) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hashed = digest.digest(key.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(12);
            for (int i = 0; i < 6; i++) {
                sb.append(String.format("%02x", hashed[i]));
            }
            return sb.toString();
        } catch (Exception e) {
            return "unavailable";
        }
    }
}
