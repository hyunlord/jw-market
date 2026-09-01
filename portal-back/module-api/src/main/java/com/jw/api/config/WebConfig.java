package com.jw.api.config;

import com.jw.api.config.ratelimit.MarketChatStreamRateLimitInterceptor;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class WebConfig implements WebMvcConfigurer {

    /**
     * MARKET Chat SSE 스트리밍 경로에만 적용되는 계정별 요청 빈도 제한.
     * 프로필 조건을 걸지 않는다 — 배포 워크로드가 전부 {@code SPRING_PROFILES_ACTIVE=dev} 라
     * {@code @Profile("prod")} 로 묶으면 실제로는 동작하지 않는다.
     */
    private static final String MARKET_CHAT_STREAM_PATH = "/api/*/market/chat/query/stream";

    private final MarketChatStreamRateLimitInterceptor marketChatStreamRateLimitInterceptor;

    public WebConfig(MarketChatStreamRateLimitInterceptor marketChatStreamRateLimitInterceptor) {
        this.marketChatStreamRateLimitInterceptor = marketChatStreamRateLimitInterceptor;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(marketChatStreamRateLimitInterceptor)
            .addPathPatterns(MARKET_CHAT_STREAM_PATH);
    }

    @Override
    public void addResourceHandlers(ResourceHandlerRegistry registry) {
        var projectRoot   = System.getProperty("user.dir");
        var resourcesPath = "file:" + projectRoot + "/module-api/src/main/resources/static/";

        registry.addResourceHandler("/**")
            .addResourceLocations(resourcesPath)
            .setCachePeriod(3600);
    }
}
