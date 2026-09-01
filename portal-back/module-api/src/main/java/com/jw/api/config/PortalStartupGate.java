package com.jw.api.config;

import com.jw.service.user.config.PortalAuthProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;

@Component
public class PortalStartupGate implements ApplicationRunner {

    private static final Logger log =
        LoggerFactory.getLogger(PortalStartupGate.class);

    private final PortalAuthProperties authProperties;
    private final boolean localNoAuth;
    private final boolean testLoginEnabled;
    private final String marketApiEndpoint;
    private final String springProfiles;

    public PortalStartupGate(
        PortalAuthProperties authProperties,
        @Value("${portal.local-no-auth:false}") boolean localNoAuth,
        @Value("${portal.test-login-enabled:false}")
        boolean testLoginEnabled,
        @Value("${rest.api.agent.market.base-url}")
        String marketApiEndpoint,
        @Value("${spring.profiles.active:local}") String springProfiles
    ) {
        this.authProperties = authProperties;
        this.localNoAuth = localNoAuth;
        this.testLoginEnabled = testLoginEnabled;
        this.marketApiEndpoint = marketApiEndpoint;
        this.springProfiles = springProfiles;
    }

    @Override
    public void run(ApplicationArguments args) {
        run();
    }

    void run() {
        authProperties.validate();
        String environment = authProperties.getEnvironment();
        boolean localRuntime = "local".equals(environment)
            && java.util.Arrays.stream(springProfiles.split(","))
                .map(String::trim)
                .allMatch("local"::equals);
        if (!localRuntime && (localNoAuth || testLoginEnabled)) {
            String enabledFlag = localNoAuth
                ? "portal.local-no-auth"
                : "portal.test-login-enabled";
            throw new IllegalStateException(
                enabledFlag
                    + " must be false outside the local environment"
            );
        }
        log.info(
            "portal_startup environment={} market_api_endpoint={} "
                + "iam_role_names={}",
            environment,
            marketApiEndpoint,
            authProperties.activeRoleDescriptions()
        );
    }
}
