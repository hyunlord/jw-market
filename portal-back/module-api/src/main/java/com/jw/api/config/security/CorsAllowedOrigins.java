package com.jw.api.config.security;

import java.net.URI;
import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class CorsAllowedOrigins {

    private final List<String> values;

    public CorsAllowedOrigins(
        @Value("${portal.security.cors.allowed-origins}") List<String> values
    ) {
        if (values == null || values.isEmpty()) {
            throw new IllegalArgumentException(
                "portal.security.cors.allowed-origins must be configured"
            );
        }
        List<String> normalized = values.stream()
            .map(origin -> origin == null ? "" : origin.trim())
            .toList();
        if (normalized.stream().anyMatch(origin -> !isExplicitHttpsOrigin(origin))) {
            throw new IllegalArgumentException(
                "portal.security.cors.allowed-origins must contain explicit HTTPS origins without wildcards or paths"
            );
        }
        this.values = List.copyOf(normalized);
    }

    public List<String> values() {
        return values;
    }

    private static boolean isExplicitHttpsOrigin(String origin) {
        try {
            URI uri = URI.create(origin);
            return "https".equals(uri.getScheme())
                && uri.getHost() != null
                && !uri.getHost().isBlank()
                && uri.getUserInfo() == null
                && (uri.getPath() == null || uri.getPath().isEmpty())
                && uri.getQuery() == null
                && uri.getFragment() == null
                && !origin.contains("*");
        } catch (IllegalArgumentException exception) {
            return false;
        }
    }
}
