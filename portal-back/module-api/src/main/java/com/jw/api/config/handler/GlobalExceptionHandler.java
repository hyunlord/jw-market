package com.jw.api.config.handler;

import com.jw.core.base.BaseEnum;
import com.jw.core.base.response.ExceptionResponse;
import com.jw.core.util.TextCore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.annotation.Order;
import org.springframework.core.env.Environment;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.authorization.AuthorizationDeniedException;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.servlet.NoHandlerFoundException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.reactive.function.client.WebClientRequestException;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

@RestControllerAdvice
@Order(1)
public class GlobalExceptionHandler {

    private static final Logger logger = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    private final Environment environment;

    public GlobalExceptionHandler(Environment environment) {
        this.environment = environment;
    }

    /**
     * Portal login role denied
     */
    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<ExceptionResponse> handleAccessDeniedException(
            AccessDeniedException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
            ex.getMessage(),
            BaseEnum.Response.Status.UNAUTHORIZED,
            "권한 없음",
            HttpStatus.FORBIDDEN.value(),
            ex.getMessage()
        );

        return ResponseEntity.status(HttpStatus.FORBIDDEN)
            .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * Access Denied
     */
    @ExceptionHandler(AuthorizationDeniedException.class)
    public ResponseEntity<ExceptionResponse> handleAuthorizationDeniedException(
            AuthorizationDeniedException e,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("type", "Access Denied");
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.UNAUTHORIZED,
                "자격 요건이 불충분 합니다.",
                401,
                detail.toString()
        );

        return ResponseEntity.badRequest()
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * NullPointerException
     */
    @ExceptionHandler(NullPointerException.class)
    public ResponseEntity<ExceptionResponse> handleNullPointerException(
            NullPointerException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                "내부 처리 중 null 참조 오류가 발생했습니다.",
                500,
                detail.toString()
        );

        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * IllegalArgumentException
     */
    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ExceptionResponse> handleIllegalArgumentException(
            IllegalArgumentException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                ex.getMessage() != null ? ex.getMessage() : "잘못된 인수가 제공되었습니다.",
                500,
                detail.toString()
        );

        return ResponseEntity.badRequest()
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * 잘못된 HTTP 메서드 요청
     */
    @ExceptionHandler(HttpRequestMethodNotSupportedException.class)
    public ResponseEntity<ExceptionResponse> handleMethodNotSupported(
            HttpRequestMethodNotSupportedException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("method", ex.getMethod());
        detail.put("supportedMethods", ex.getSupportedMethods() != null
                ? Arrays.asList(ex.getSupportedMethods())
                : Collections.emptyList());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));
        detail.put("userAgent", telemetryValue(request.getHeader("User-Agent")));
        detail.put(
            "portalBundle",
            telemetryValue(request.getHeader("X-Portal-Bundle"))
        );

        logger.warn(
            "portal_method_not_allowed method={} path={} allow={} user_agent={} "
                + "portal_bundle={} trace_id={}",
            ex.getMethod(),
            detail.get("path"),
            detail.get("supportedMethods"),
            detail.get("userAgent"),
            detail.get("portalBundle"),
            traceId
        );

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                "지원하지 않는 HTTP 메서드입니다: " + ex.getMethod(),
                HttpStatus.METHOD_NOT_ALLOWED.value(),
                detail.toString()
        );

        return ResponseEntity.status(HttpStatus.METHOD_NOT_ALLOWED)
                .allow(ex.getSupportedHttpMethods() != null
                    ? ex.getSupportedHttpMethods().toArray(HttpMethod[]::new)
                    : new HttpMethod[0])
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    @ExceptionHandler({
        NoHandlerFoundException.class,
        NoResourceFoundException.class
    })
    public ResponseEntity<ExceptionResponse> handleNoResourceFound(
        Exception ex,
        WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
            null,
            BaseEnum.Response.Status.FAIL,
            "요청한 경로를 찾을 수 없습니다.",
            HttpStatus.NOT_FOUND.value(),
            detail.toString()
        );

        return ResponseEntity.status(HttpStatus.NOT_FOUND)
            .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * 요청 매개변수 누락
     */
    @ExceptionHandler(MissingServletRequestParameterException.class)
    public ResponseEntity<ExceptionResponse> handleMissingRequestParameter(
            MissingServletRequestParameterException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("parameter", ex.getParameterName());
        detail.put("type", ex.getParameterType());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                "필수 요청 매개변수가 누락되었습니다: " + ex.getParameterName(),
                500,
                detail.toString()
        );

        return ResponseEntity.badRequest()
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * 필수 매개변수 값 null
     */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<ExceptionResponse> handleMessageNotReadable(
            HttpMessageNotReadableException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                "필수 요청유형, 필수 매개변수 값 null 또는 문법이 잘못 되었습니다.",
                500,
                detail.toString()
        );

        return ResponseEntity.badRequest()
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * 요청 매개변수 유형 불일치
     */
    @ExceptionHandler(MethodArgumentTypeMismatchException.class)
    public ResponseEntity<ExceptionResponse> handleMethodArgumentTypeMismatch(
            MethodArgumentTypeMismatchException ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                "필수 요청 매개변수 유형 일치하지 않습니다.",
                500,
                detail.toString()
        );

        return ResponseEntity.badRequest()
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * WebClient 네트워크 오류 (연결 실패, 타임아웃)
     * ex) WebClientRequestException
     */
    @ExceptionHandler(WebClientRequestException.class)
    public ResponseEntity<ExceptionResponse> handleWebClientRequestException(
        WebClientRequestException ex,
        WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("traceId", traceId);
        detail.put("path",    extractPath(request));
        detail.put("uri",     ex.getUri().toString());

        ExceptionResponse errorResponse = new ExceptionResponse(
            null,
            BaseEnum.Response.Status.FAIL,
            "외부 서버에 연결할 수 없습니다.",
            502,
            detail.toString()
        );

        return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
            .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * WebClient 응답 오류 (4xx / 5xx)
     * ex) WebClientResponseException$BadRequest
     *     WebClientResponseException$Unauthorized
     *     WebClientResponseException$InternalServerError
     */
    @ExceptionHandler(WebClientResponseException.class)
    public ResponseEntity<ExceptionResponse> handleWebClientResponseException(
        WebClientResponseException ex,
        WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message",    ex.getMessage());
        detail.put("traceId",    traceId);
        detail.put("path",       extractPath(request));

        HttpStatus httpStatus = HttpStatus.resolve(ex.getStatusCode().value()) != null
            ? HttpStatus.resolve(ex.getStatusCode().value())
            : HttpStatus.BAD_GATEWAY;

        String userMessage = switch (ex.getStatusCode().value()) {
            case 400 -> "잘못된 요청입니다.";
            case 401 -> "인증이 만료되었습니다.";
            case 403 -> "접근 권한이 없습니다.";
            case 404 -> "요청한 데이터를 찾을 수 없습니다.";
            case 408 -> "외부 서버 요청 시간이 초과되었습니다.";
            default  -> ex.getStatusCode().is5xxServerError()
                ? "외부 서버 오류가 발생했습니다."
                : "외부 서버와 통신 중 오류가 발생했습니다.";
        };

        ExceptionResponse errorResponse = new ExceptionResponse(
            extractResponseBody(ex),
            BaseEnum.Response.Status.FAIL,
            userMessage,
            httpStatus.value(),
            detail.toString()
        );

        return ResponseEntity.status(httpStatus)
            .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    /**
     * Exception
     */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<ExceptionResponse> handleGeneralException(
            Exception ex,
            WebRequest request
    ) {
        String activeProfile = this.getActiveProfile();
        String traceId = TextCore.uuidV1().substring(0, 10);

        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("traceId", traceId);
        detail.put("path", extractPath(request));

        ExceptionResponse errorResponse = new ExceptionResponse(
                null,
                BaseEnum.Response.Status.FAIL,
                "서버에서 오류가 발생했습니다.",
                500,
                detail.toString()
        );

        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(this.isProd(activeProfile) ? null : errorResponse);
    }

    private String getActiveProfile() {
        String[] profiles = environment.getActiveProfiles();
        return (profiles.length > 0) ? profiles[0] : "local";
    }

    private boolean isProd(String activeProfile) {
        return BaseEnum.Profile.PROD.getCode().equalsIgnoreCase(activeProfile);
    }

    private String extractPath(WebRequest request) {
        return request.getDescription(false).replace("uri=", "");
    }

    private Object extractResponseBody(WebClientResponseException ex) {
        try {
            return ex.getResponseBodyAs(Map.class);
        } catch (Exception parseEx) {
            String raw = ex.getResponseBodyAsString();
            return (raw == null || raw.isBlank()) ? null : raw;
        }
    }

    private String telemetryValue(String value) {
        if (value == null) {
            return "";
        }
        String normalized = value.replaceAll("[\\r\\n\\t]", " ");
        return normalized.substring(0, Math.min(normalized.length(), 256));
    }

    public static class ParameterInfo {
        private final String methodName;
        private final String parameterName;

        public ParameterInfo(String methodName, String parameterName) {
            this.methodName = methodName;
            this.parameterName = parameterName;
        }

        public String getMethodName()    { return methodName; }
        public String getParameterName() { return parameterName; }
    }
}
