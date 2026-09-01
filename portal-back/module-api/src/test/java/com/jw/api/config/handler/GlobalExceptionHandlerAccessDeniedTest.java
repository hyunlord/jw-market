package com.jw.api.config.handler;

import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.web.context.request.WebRequest;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class GlobalExceptionHandlerAccessDeniedTest {

    @Test
    void portalRoleDenialReturnsForbiddenWithAUserFacingMessage() {
        MockEnvironment environment = new MockEnvironment();
        environment.setActiveProfiles("dev");
        GlobalExceptionHandler handler =
            new GlobalExceptionHandler(environment);
        WebRequest request = mock(WebRequest.class);
        when(request.getDescription(false)).thenReturn(
            "uri=/api/v1/auth/login"
        );

        var response = handler.handleAccessDeniedException(
            new AccessDeniedException("등록된 포탈 권한이 없습니다."),
            request
        );

        assertThat(response.getStatusCode().value()).isEqualTo(403);
        assertThat(response.getBody()).isNotNull();
        assertThat(response.getBody().result())
            .isEqualTo("등록된 포탈 권한이 없습니다.");
        assertThat(response.getBody().statusCode()).isEqualTo(403);
    }
}
