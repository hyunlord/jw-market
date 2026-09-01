package com.jw.api.config.security;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

import com.jw.core.auth.jwt.JwtAuthenticationEntryPoint;
import com.jw.core.auth.jwt.JwtAuthenticationFilter;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.web.cors.CorsConfiguration;

class SecurityConfigCorsTest {

    private static final List<String> ALLOWED_ORIGINS = List.of(
        "https://admin.dev.ai.jwhealthcare.com",
        "https://jwai-dev.jwhealthcare.com",
        "https://jwai.jwhealthcare.com"
    );

    @Test
    void arbitraryCredentialedOriginIsRejected() {
        CorsConfiguration configuration = configuration();

        assertThat(configuration.checkOrigin("https://attacker.invalid"))
            .isNull();
        assertThat(configuration.getAllowCredentials()).isTrue();
    }

    @Test
    void configuredOriginsAndPreflightMethodAreAllowed() {
        CorsConfiguration configuration = configuration();

        assertThat(configuration.checkOrigin(ALLOWED_ORIGINS.getFirst()))
            .isEqualTo(ALLOWED_ORIGINS.getFirst());
        assertThat(configuration.checkHttpMethod(HttpMethod.OPTIONS))
            .contains(HttpMethod.OPTIONS);
    }

    @Test
    void missingOrWildcardOriginsFailClosed() {
        assertThat(org.assertj.core.api.Assertions.catchThrowable(() -> config(List.of())))
            .isInstanceOf(IllegalArgumentException.class);
        assertThat(org.assertj.core.api.Assertions.catchThrowable(() -> config(List.of("*"))))
            .isInstanceOf(IllegalArgumentException.class);
        assertThat(org.assertj.core.api.Assertions.catchThrowable(() ->
            config(List.of("https://jwai.jwhealthcare.com/path"))))
            .isInstanceOf(IllegalArgumentException.class);
    }

    private static CorsConfiguration configuration() {
        SecurityConfig securityConfig = config(ALLOWED_ORIGINS);
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.setRequestURI("/api/v1/auth/google/login");
        request.addHeader(HttpHeaders.ORIGIN, ALLOWED_ORIGINS.getFirst());
        return securityConfig.corsConfigurationSource()
            .getCorsConfiguration(request);
    }

    private static SecurityConfig config(List<String> origins) {
        return new SecurityConfig(
            "bcrypt",
            mock(JwtAuthenticationFilter.class),
            mock(JwtAuthenticationEntryPoint.class),
            new CorsAllowedOrigins(origins)
        );
    }
}
