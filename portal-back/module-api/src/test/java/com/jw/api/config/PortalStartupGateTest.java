package com.jw.api.config;

import com.jw.service.user.config.PortalAuthProperties;
import org.junit.jupiter.api.Test;
import org.springframework.boot.context.properties.bind.Bindable;
import org.springframework.boot.context.properties.bind.Binder;
import org.springframework.boot.env.YamlPropertySourceLoader;
import org.springframework.core.env.StandardEnvironment;
import org.springframework.core.io.ClassPathResource;
import org.springframework.beans.factory.annotation.Value;

import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class PortalStartupGateTest {

    @Test
    void startupMarketEndpointUsesTheMarketClientProperty() {
        List<String> injectedProperties = Arrays.stream(
            PortalStartupGate.class.getConstructors()[0].getParameters()
        )
            .map(parameter -> parameter.getAnnotation(Value.class))
            .filter(java.util.Objects::nonNull)
            .map(Value::value)
            .toList();

        assertThat(injectedProperties)
            .contains("${rest.api.agent.market.base-url}")
            .doesNotContain("${rest.api.agent.jwai.base-url}");
    }

    @Test
    void devRejectsLocalNoAuthBypass() {
        PortalAuthProperties properties = validProperties("dev");

        PortalStartupGate gate = new PortalStartupGate(
            properties,
            true,
            false,
            "https://admin.dev.ai.jwhealthcare.com",
            "dev"
        );

        assertThatThrownBy(gate::run)
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("portal.local-no-auth");
    }

    @Test
    void devRejectsLegacyTestLoginBypass() {
        PortalAuthProperties properties = validProperties("dev");

        PortalStartupGate gate = new PortalStartupGate(
            properties,
            false,
            true,
            "https://admin.dev.ai.jwhealthcare.com",
            "dev"
        );

        assertThatThrownBy(gate::run)
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("portal.test-login-enabled");
    }

    @Test
    void stageAcceptsExistingMappingsWithBypassesDisabled() {
        new PortalStartupGate(
            validProperties("stage"),
            false,
            false,
            "https://admin.dev.ai.jwhealthcare.com",
            "dev"
        ).run();
    }

    @Test
    void devSpringProfileRejectsBypassEvenIfPortalEnvClaimsLocal() {
        PortalAuthProperties properties = validProperties("local");

        PortalStartupGate gate = new PortalStartupGate(
            properties,
            true,
            false,
            "https://admin.dev.ai.jwhealthcare.com",
            "dev"
        );

        assertThatThrownBy(gate::run)
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("portal.local-no-auth");
    }

    @Test
    void applicationYamlPreservesStageAndDefinesDevMappings()
        throws Exception {
        StandardEnvironment environment = new StandardEnvironment();
        YamlPropertySourceLoader loader = new YamlPropertySourceLoader();
        loader.load(
            "portal",
            new ClassPathResource("application.yaml")
        ).forEach(
            propertySource ->
                environment.getPropertySources().addLast(propertySource)
        );

        PortalAuthProperties properties = Binder.get(environment)
            .bind("portal", Bindable.of(PortalAuthProperties.class))
            .orElseThrow(
                () -> new IllegalStateException(
                    "portal properties were not bound"
                )
            );

        properties.setEnvironment("stage");
        org.assertj.core.api.Assertions.assertThat(
            properties.resolveCodes(
                List.of("관리자 역할", "R&D Agent", "시장분석 Agent")
            )
        ).containsExactly("ALL", "RND", "MARKET");

        properties.setEnvironment("dev");
        org.assertj.core.api.Assertions.assertThat(
            properties.resolveCodes(List.of("포탈 개발자"))
        ).containsExactly("ALL");
        org.assertj.core.api.Assertions.assertThat(
            properties.resolveCodes(List.of("테스트 관리 그룹 역할"))
        ).isEmpty();
    }

    private static PortalAuthProperties validProperties(String environmentName) {
        PortalAuthProperties.RoleMapping mapping =
            new PortalAuthProperties.RoleMapping();
        mapping.setDescription(
            environmentName.equals("dev")
                ? "포탈 개발자"
                : "관리자 역할"
        );
        mapping.setCode("ALL");

        PortalAuthProperties.EnvironmentMapping environment =
            new PortalAuthProperties.EnvironmentMapping();
        environment.setRoleMappings(List.of(mapping));

        PortalAuthProperties properties = new PortalAuthProperties();
        properties.setEnvironment(environmentName);
        properties.setEnvironments(Map.of(environmentName, environment));
        return properties;
    }
}
