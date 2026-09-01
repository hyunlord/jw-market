package com.jw.api.config;

import io.swagger.v3.oas.models.Components;
import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Contact;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.info.License;
import io.swagger.v3.oas.models.security.SecurityRequirement;
import io.swagger.v3.oas.models.security.SecurityScheme;
import io.swagger.v3.oas.models.servers.Server;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.List;

@Configuration
public class SwaggerConfig {

    @Value("${server.port:8080}")
    private String serverPort;

    @Bean
    public OpenAPI openAPI() {
        return new OpenAPI()
            .info(apiInfo())
            .servers(apiServers())
            .addSecurityItem(new SecurityRequirement().addList("Bearer Authentication"))
            .components(
                new Components()
                    .addSecuritySchemes("Bearer Authentication", createAPIKeyScheme())
            );
    }

    private Info apiInfo() {
        var description = """
            ## JW PORTAL REST API 문서
            
            이 API는 JWT 토큰 기반 인증을 사용합니다.
            
            ### 인증 방법:
            1. `/api/v1/auth/login` 엔드포인트로 로그인
            2. 응답으로 받은 `accessToken`을 복사
            3. 페이지 상단의 🔒 "Authorize" 버튼 클릭
            4. Value 필드에 `Bearer {토큰}` 형태로 입력
            5. "Authorize" 버튼 클릭
            
            ### 기본 사용자:
            - **사용자**: [ROLE_USER]
            
            ### API 버전:
            - **현재 버전**: v1
            - **지원 형식**: JSON
            """;

        return new Info()
            .title("JW PORTAL API")
            .description(description)
            .version("1.0.0")
            .contact(
                new Contact()
                    .name("JW PORTAL Team")
                    .email("ai@jwhealthcare.co.kr")
                    .url("https://jwai-dev.jwhealthcare.com")
            )
            .license(
                new License()
                    .name("MIT License")
                    .url("https://opensource.org/licenses/MIT")
            );
    }
    
    private List<Server> apiServers() {
        return List.of(
            new Server()
                .url("https://jwai-dev.jwhealthcare.com")
                .description("개발 서버"),
            new Server()
                .url("http://localhost:" + serverPort)
                .description("로컬 개발 서버")
        );
    }
    
    private SecurityScheme createAPIKeyScheme() {
        return new SecurityScheme()
            .type(SecurityScheme.Type.HTTP)
            .scheme("bearer")
            .bearerFormat("JWT")
            .description("JWT 토큰을 입력하세요. 'Bearer '는 자동으로 추가됩니다.");
    }
    
}
