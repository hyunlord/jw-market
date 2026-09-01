package com.jw.api.config.ratelimit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.jw.core.auth.jwt.JwtTokenProvider;
import jakarta.servlet.DispatcherType;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

/**
 * 인터셉터 계층 검증. 순수 카운터 산술은
 * {@code com.jw.core.ratelimit.SlidingWindowRateLimiterTest} 가 담당한다.
 */
class MarketChatStreamRateLimitInterceptorTest {

    private static final String PATH = "/api/v1/market/chat/query/stream";

    /** MeterRegistry 없이 동작해야 한다 (선택 의존성). */
    private static final ObjectProvider<io.micrometer.core.instrument.MeterRegistry> NO_METER =
        new ObjectProvider<>() {
            @Override
            public io.micrometer.core.instrument.MeterRegistry getObject() {
                throw new UnsupportedOperationException();
            }

            @Override
            public io.micrometer.core.instrument.MeterRegistry getObject(Object... args) {
                throw new UnsupportedOperationException();
            }

            @Override
            public io.micrometer.core.instrument.MeterRegistry getIfAvailable() {
                return null;
            }

            @Override
            public io.micrometer.core.instrument.MeterRegistry getIfUnique() {
                return null;
            }
        };

    private static String tokenWithUid(long uid) {
        return jwt("{\"sub\":\"t\",\"uid\":" + uid + ",\"token_type\":\"access\"}");
    }

    private static String jwt(String payloadJson) {
        Base64.Encoder enc = Base64.getUrlEncoder().withoutPadding();
        String header = enc.encodeToString("{\"alg\":\"HS256\"}".getBytes(StandardCharsets.UTF_8));
        String payload = enc.encodeToString(payloadJson.getBytes(StandardCharsets.UTF_8));
        return header + "." + payload + ".sig";
    }

    private static MarketChatStreamRateLimitInterceptor interceptor(boolean enabled, int perMinute) {
        return new MarketChatStreamRateLimitInterceptor(
            enabled,
            perMinute,
            1000,
            new JwtTokenProvider(
                "test-secret-key-that-is-long-enough-for-hs256-signing",
                "jw-portal-api",
                "jw-portal-web",
                3600,
                7200
            ),
            NO_METER
        );
    }

    private static MockHttpServletRequest request(String token) {
        var req = new MockHttpServletRequest("POST", PATH);
        req.setRemoteAddr("10.0.0.7");
        if (token != null) {
            req.addHeader("Authorization-Access-Token", token);
        }
        return req;
    }

    @Test
    void allowsUpToLimitThenReturns429WithRetryAfter() throws Exception {
        var it = interceptor(true, 2);
        // traceId/숫자와 우연히 겹치지 않도록 특징적인 uid 를 쓴다
        String token = tokenWithUid(987654321L);

        assertTrue(it.preHandle(request(token), new MockHttpServletResponse(), new Object()));
        assertTrue(it.preHandle(request(token), new MockHttpServletResponse(), new Object()));

        var blocked = new MockHttpServletResponse();
        assertFalse(it.preHandle(request(token), blocked, new Object()));

        assertEquals(429, blocked.getStatus());
        assertEquals("2", blocked.getHeader("X-RateLimit-Limit"));
        assertEquals("0", blocked.getHeader("X-RateLimit-Remaining"));
        assertTrue(Integer.parseInt(blocked.getHeader("Retry-After")) >= 1);
        assertTrue(blocked.getContentType().startsWith("application/json"));

        String body = blocked.getContentAsString();
        assertTrue(body.contains("\"statusCode\":429"), body);
        assertTrue(body.contains("\"status\":\"FAIL\""), body);
        // 계정 식별자가 응답 본문으로 새지 않아야 한다
        assertFalse(body.contains("987654321"), "uid 가 본문에 노출되면 안 된다: " + body);
    }

    @Test
    void allowedRequestIsLeftCompletelyUntouched() throws Exception {
        var it = interceptor(true, 5);
        var res = new MockHttpServletResponse();

        assertTrue(it.preHandle(request(tokenWithUid(12L)), res, new Object()));

        assertEquals(200, res.getStatus(), "상태코드 무변경");
        assertTrue(res.getHeaderNames().isEmpty(), "한도 내 응답에 헤더를 추가하면 안 된다: " + res.getHeaderNames());
        assertNull(res.getHeader("X-RateLimit-Limit"));
        assertNull(res.getHeader("Retry-After"));
        assertEquals("", res.getContentAsString(), "본문 무변경");
    }

    /**
     * ★ 회귀 고정 — SSE(Flux) 핸들러는 완료 시 DispatcherType.ASYNC 로 재디스패치되며
     * preHandle 이 다시 호출된다. 재디스패치를 세면 스트리밍 요청 1건이 카운터를 2개 먹고,
     * 차단 시 이미 커밋된 응답을 잘라먹는다. 최초 디스패치에서만 판정해야 한다.
     */
    @Test
    void asyncRedispatchIsNotCountedAndNeverBlocks() throws Exception {
        var it = interceptor(true, 2);
        String token = tokenWithUid(13L);

        // 최초 디스패치 2건 = 한도 소진
        assertTrue(it.preHandle(request(token), new MockHttpServletResponse(), new Object()));
        assertTrue(it.preHandle(request(token), new MockHttpServletResponse(), new Object()));

        // 두 스트림의 ASYNC 재디스패치는 카운트되지 않고 통과해야 한다
        for (int i = 0; i < 2; i++) {
            var async = request(token);
            async.setDispatcherType(DispatcherType.ASYNC);
            var res = new MockHttpServletResponse();
            assertTrue(it.preHandle(async, res, new Object()),
                "ASYNC 재디스패치는 절대 차단되면 안 된다");
            assertEquals(200, res.getStatus());
            assertTrue(res.getHeaderNames().isEmpty());
        }

        // 그래도 3번째 '최초' 요청은 여전히 차단된다 (가드가 한도를 무력화하지 않음)
        var blocked = new MockHttpServletResponse();
        assertFalse(it.preHandle(request(token), blocked, new Object()));
        assertEquals(429, blocked.getStatus());
    }

    @Test
    void accountsAreIsolated() throws Exception {
        var it = interceptor(true, 1);

        assertTrue(it.preHandle(request(tokenWithUid(21L)), new MockHttpServletResponse(), new Object()));
        assertFalse(it.preHandle(request(tokenWithUid(21L)), new MockHttpServletResponse(), new Object()));
        // 다른 계정은 영향 없음
        assertTrue(it.preHandle(request(tokenWithUid(22L)), new MockHttpServletResponse(), new Object()));
    }

    @Test
    void keylessRequestsFallBackToPerIpBucketNotAGlobalOne() throws Exception {
        var it = interceptor(true, 1);

        var first = new MockHttpServletRequest("POST", PATH);
        first.setRemoteAddr("10.0.0.1");
        assertTrue(it.preHandle(first, new MockHttpServletResponse(), new Object()));

        var sameIp = new MockHttpServletRequest("POST", PATH);
        sameIp.setRemoteAddr("10.0.0.1");
        assertFalse(it.preHandle(sameIp, new MockHttpServletResponse(), new Object()),
            "같은 IP 의 두 번째 무키 요청은 차단된다");

        // ★ 다른 IP 는 별도 버킷 — 전역 단일 버킷이면 여기서 막힌다
        var otherIp = new MockHttpServletRequest("POST", PATH);
        otherIp.setRemoteAddr("10.0.0.2");
        assertTrue(it.preHandle(otherIp, new MockHttpServletResponse(), new Object()),
            "익명 요청이 전역 단일 버킷으로 붕괴하면 안 된다");
    }

    @Test
    void malformedTokenIsDemotedToAnonymousInsteadOfThrowing() throws Exception {
        var it = interceptor(true, 1);

        var req = request("not-a-jwt");
        assertTrue(it.preHandle(req, new MockHttpServletResponse(), new Object()));

        // 같은 IP 의 uid 없는 토큰과 같은 버킷을 쓴다 → 두 번째는 차단
        var req2 = request(jwt("{\"sub\":\"t\",\"token_type\":\"access\"}"));
        assertFalse(it.preHandle(req2, new MockHttpServletResponse(), new Object()));
    }

    @Test
    void xForwardedForFirstHopWinsOverRemoteAddr() throws Exception {
        var it = interceptor(true, 1);

        var a = new MockHttpServletRequest("POST", PATH);
        a.setRemoteAddr("10.0.0.9");
        a.addHeader("X-Forwarded-For", "203.0.113.5, 10.0.0.9");
        assertTrue(it.preHandle(a, new MockHttpServletResponse(), new Object()));

        var b = new MockHttpServletRequest("POST", PATH);
        b.setRemoteAddr("10.0.0.9");
        b.addHeader("X-Forwarded-For", "203.0.113.6, 10.0.0.9");
        assertTrue(it.preHandle(b, new MockHttpServletResponse(), new Object()),
            "remoteAddr 이 같아도 XFF 첫 홉이 다르면 다른 버킷");

        var c = new MockHttpServletRequest("POST", PATH);
        c.setRemoteAddr("10.0.0.9");
        c.addHeader("X-Forwarded-For", "203.0.113.5, 10.0.0.9");
        assertFalse(it.preHandle(c, new MockHttpServletResponse(), new Object()),
            "같은 XFF 첫 홉은 같은 버킷");
    }

    @Test
    void disabledFlagBypassesEverything() throws Exception {
        var it = interceptor(false, 1);
        String token = tokenWithUid(31L);

        for (int i = 0; i < 5; i++) {
            var res = new MockHttpServletResponse();
            assertTrue(it.preHandle(request(token), res, new Object()));
            assertEquals(200, res.getStatus());
        }
    }

    @Test
    void doesNotWriteOntoAnAlreadyCommittedResponse() throws Exception {
        var it = interceptor(true, 1);
        String token = tokenWithUid(41L);

        assertTrue(it.preHandle(request(token), new MockHttpServletResponse(), new Object()));

        var committed = new MockHttpServletResponse();
        committed.getWriter().write("event:delta\ndata:already streaming\n\n");
        committed.flushBuffer();
        assertTrue(committed.isCommitted());

        assertFalse(it.preHandle(request(token), committed, new Object()),
            "차단 판정 자체는 유지된다");
        assertEquals(200, committed.getStatus(), "커밋된 응답의 상태코드를 덮어쓰면 안 된다");
        assertTrue(committed.getContentAsString().contains("already streaming"),
            "진행 중이던 본문을 훼손하면 안 된다");
    }
}
