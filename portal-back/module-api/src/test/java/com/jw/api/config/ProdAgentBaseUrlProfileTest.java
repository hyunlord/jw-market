package com.jw.api.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import com.jw.service.config.ProdAgentBaseUrlRequirements;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.WebApplicationType;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;

class ProdAgentBaseUrlProfileTest {

    private static final String JWAI_ENV = "PORTAL_PROD_JWAI_BASE_URL";
    private static final String MARKET_ENV = "PORTAL_PROD_MARKET_BASE_URL";
    private static final String JWAI_VALUE = "https://prod-jwai.invalid";
    private static final String MARKET_VALUE = "https://prod-market.invalid";
    private static final String DEV_JWAI_VALUE = "https://admin.dev.ai.jwhealthcare.com";
    private static final String DEV_MARKET_VALUE = "https://jwai-dev.jwhealthcare.com";

    @Test
    void prodProfileFailsWhenJwaiBaseUrlIsMissing() {
        assertThatThrownBy(() -> run("prod", Map.of(MARKET_ENV, MARKET_VALUE)))
            .hasRootCauseMessage("PORTAL_PROD_JWAI_BASE_URL must be set");
    }

    @Test
    void prodProfileFailsWhenMarketBaseUrlIsMissing() {
        assertThatThrownBy(() -> run("prod", Map.of(JWAI_ENV, JWAI_VALUE)))
            .hasRootCauseMessage("PORTAL_PROD_MARKET_BASE_URL must be set");
    }

    @Test
    void prodProfileFailsWhenJwaiBaseUrlIsBlank() {
        assertThatThrownBy(() -> run("prod", Map.of(
            JWAI_ENV, " ",
            MARKET_ENV, MARKET_VALUE
        )))
            .hasRootCauseMessage("PORTAL_PROD_JWAI_BASE_URL must be set");
    }

    @Test
    void prodProfileFailsWhenMarketBaseUrlIsBlank() {
        assertThatThrownBy(() -> run("prod", Map.of(
            JWAI_ENV, JWAI_VALUE,
            MARKET_ENV, " "
        )))
            .hasRootCauseMessage("PORTAL_PROD_MARKET_BASE_URL must be set");
    }

    @Test
    void prodRequirementsRejectUnresolvedJwaiPlaceholder() {
        assertThatThrownBy(() -> new ProdAgentBaseUrlRequirements(
            "${PORTAL_PROD_JWAI_BASE_URL}", MARKET_VALUE
        ))
            .hasMessage("PORTAL_PROD_JWAI_BASE_URL must be set");
    }

    @Test
    void prodRequirementsRejectUnresolvedMarketPlaceholder() {
        assertThatThrownBy(() -> new ProdAgentBaseUrlRequirements(
            JWAI_VALUE, "${PORTAL_PROD_MARKET_BASE_URL}"
        ))
            .hasMessage("PORTAL_PROD_MARKET_BASE_URL must be set");
    }

    @Test
    void prodProfileUsesExplicitBaseUrlsWhenBothArePresent() {
        try (ConfigurableApplicationContext context = run("prod", Map.of(
            JWAI_ENV, JWAI_VALUE,
            MARKET_ENV, MARKET_VALUE
        ))) {
            RequiredAgentBaseUrls urls = context.getBean(RequiredAgentBaseUrls.class);

            assertThat(urls.jwai()).isEqualTo(JWAI_VALUE);
            assertThat(urls.market()).isEqualTo(MARKET_VALUE);
        }
    }

    @Test
    void devProfileDoesNotRequireProdBaseUrlEnvironment() {
        try (ConfigurableApplicationContext context = run("dev", Map.of())) {
            RequiredAgentBaseUrls urls = context.getBean(RequiredAgentBaseUrls.class);

            assertThat(urls.jwai()).isEqualTo(DEV_JWAI_VALUE);
            assertThat(urls.market()).isEqualTo(DEV_MARKET_VALUE);
        }
    }

    private ConfigurableApplicationContext run(String profile, Map<String, String> environment) {
        SpringApplication application = new SpringApplication(ProbeConfiguration.class);
        application.setWebApplicationType(WebApplicationType.NONE);
        List<String> arguments = new ArrayList<>();
        arguments.add("--spring.profiles.active=" + profile);
        arguments.add("--spring.main.banner-mode=off");
        arguments.add("--logging.level.root=off");
        environment.forEach((key, value) -> arguments.add("--" + key + "=" + value));
        return application.run(arguments.toArray(String[]::new));
    }

    @Configuration(proxyBeanMethods = false)
    @Import(ProdAgentBaseUrlRequirements.class)
    static class ProbeConfiguration {

        @Bean
        RequiredAgentBaseUrls requiredAgentBaseUrls(
            @Value("${rest.api.agent.jwai.base-url}") String jwai,
            @Value("${rest.api.agent.market.base-url}") String market
        ) {
            return new RequiredAgentBaseUrls(jwai, market);
        }
    }

    record RequiredAgentBaseUrls(String jwai, String market) {
    }
}
