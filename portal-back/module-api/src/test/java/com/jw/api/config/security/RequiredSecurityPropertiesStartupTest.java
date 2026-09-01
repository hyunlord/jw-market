package com.jw.api.config.security;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.jw.core.auth.jwt.JwtTokenProvider;
import com.jw.service.user.security.GoogleIdTokenClaimsValidator;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;

class RequiredSecurityPropertiesStartupTest {

    private static final String TEST_SECRET =
        "test-secret-key-that-is-long-enough-for-hs256-signing";
    private static final String GOOGLE_CLIENT_IDS =
        "372798032844-hidrd2q4b76uapp0st24tv1bo984v5ff.apps.googleusercontent.com," +
        "372798032844-07h2fa2560i1j71kemm6r2kp88e3jr3v.apps.googleusercontent.com";

    @Test
    void missingOrMalformedGoogleClientIdsStopStartupAndConfiguredListStarts() {
        assertThatThrownBy(() -> run(GoogleProbe.class, Map.of()))
            .hasStackTraceContaining("portal.auth.google.client-id");
        assertThatThrownBy(() -> run(GoogleProbe.class, Map.of(
            "portal.auth.google.client-id", "not-a-google-client"
        )))
            .hasStackTraceContaining("portal.auth.google.client-id");
        assertThatCode(() -> close(run(GoogleProbe.class, Map.of(
            "portal.auth.google.client-id", GOOGLE_CLIENT_IDS
        )))).doesNotThrowAnyException();
    }

    @Test
    void missingCorsOriginsStopsStartupAndConfiguredValueStarts() {
        assertThatThrownBy(() -> run(CorsProbe.class, Map.of()))
            .hasStackTraceContaining("portal.security.cors.allowed-origins");
        assertThatCode(() -> close(run(CorsProbe.class, Map.of(
            "portal.security.cors.allowed-origins",
            "https://admin.dev.ai.jwhealthcare.com"
        )))).doesNotThrowAnyException();
    }

    @Test
    void missingJwtPropertiesStopStartupAndCompleteSetStarts() {
        assertThatThrownBy(() -> run(JwtProbe.class, Map.of()))
            .hasStackTraceContaining("app.jwt.secret");
        assertThatCode(() -> close(run(JwtProbe.class, Map.of(
            "app.jwt.secret", TEST_SECRET,
            "app.jwt.issuer", "jw-portal-api",
            "app.jwt.audience", "jw-portal-web"
        )))).doesNotThrowAnyException();
    }

    private static ConfigurableApplicationContext run(
        Class<?> configuration,
        Map<String, String> properties
    ) {
        SpringApplication application = new SpringApplication(configuration);
        List<String> arguments = new ArrayList<>(List.of(
            "--spring.main.web-application-type=none",
            "--spring.main.banner-mode=off",
            "--logging.level.root=off",
            "--spring.config.location=optional:classpath:/security-test-empty.yaml"
        ));
        properties.forEach((key, value) -> arguments.add("--" + key + "=" + value));
        return application.run(arguments.toArray(String[]::new));
    }

    private static void close(ConfigurableApplicationContext context) {
        context.close();
    }

    @Configuration(proxyBeanMethods = false)
    @Import(GoogleIdTokenClaimsValidator.class)
    static class GoogleProbe {
    }

    @Configuration(proxyBeanMethods = false)
    @Import(CorsAllowedOrigins.class)
    static class CorsProbe {
    }

    @Configuration(proxyBeanMethods = false)
    @Import(JwtTokenProvider.class)
    static class JwtProbe {
    }
}
