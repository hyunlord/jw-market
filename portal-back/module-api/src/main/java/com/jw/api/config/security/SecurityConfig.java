package com.jw.api.config.security;

import com.jw.core.auth.jwt.JwtAuthenticationEntryPoint;
import com.jw.core.auth.jwt.JwtAuthenticationFilter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.config.annotation.authentication.configuration.AuthenticationConfiguration;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.oauth2.client.userinfo.DefaultOAuth2UserService;
import org.springframework.security.oauth2.client.userinfo.OAuth2UserRequest;
import org.springframework.security.oauth2.client.userinfo.OAuth2UserService;
import org.springframework.security.oauth2.core.user.OAuth2User;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;

import java.util.List;

@Configuration
@EnableWebSecurity
@EnableMethodSecurity(securedEnabled = true)
public class SecurityConfig {

    private final String passwordEncoderType;
    private final JwtAuthenticationFilter jwtAuthenticationFilter;
    private final JwtAuthenticationEntryPoint jwtAuthenticationEntryPoint;
    private final List<String> allowedCorsOrigins;

    public SecurityConfig(
        @Value("${encoder.password.type:bcrypt}") String passwordEncoderType,
        JwtAuthenticationFilter jwtAuthenticationFilter,
        JwtAuthenticationEntryPoint jwtAuthenticationEntryPoint,
        CorsAllowedOrigins allowedCorsOrigins
    ) {
        this.passwordEncoderType = passwordEncoderType;
        this.jwtAuthenticationFilter = jwtAuthenticationFilter;
        this.jwtAuthenticationEntryPoint = jwtAuthenticationEntryPoint;
        this.allowedCorsOrigins = allowedCorsOrigins.values();
    }

    private void configureCommonHeaders(HttpSecurity http) throws Exception {
        http.headers(headers -> headers
            .frameOptions(frame -> frame.deny())
            .httpStrictTransportSecurity(hsts -> hsts
                .maxAgeInSeconds(31536000)
                .includeSubDomains(true)
            )
            .permissionsPolicyHeader(policy -> policy
                .policy("camera=self, microphone=self, geolocation=none, payment=none, usb=none")
            )
        );
    }

    private void configureCommonBase(HttpSecurity http) throws Exception {
        http
            .csrf(csrf -> csrf.disable())
            .cors(cors -> cors.configurationSource(corsConfigurationSource()))
            .sessionManagement(session -> session
                .sessionCreationPolicy(SessionCreationPolicy.STATELESS)
            )
            .exceptionHandling(ex -> ex
                .authenticationEntryPoint(jwtAuthenticationEntryPoint)
            )
            .addFilterBefore(
                jwtAuthenticationFilter,
                UsernamePasswordAuthenticationFilter.class
            );
    }

    @Bean
    @Profile("local")
    public SecurityFilterChain localSecurityFilterChain(HttpSecurity http) throws Exception {

        configureCommonBase(http);
        configureCommonHeaders(http);

        http.authorizeHttpRequests(auth -> auth
            // Default
            .requestMatchers("/favicon.ico").permitAll()
            // GCP LB
            .requestMatchers("/actuator/health").permitAll()
            .requestMatchers("/actuator/health/**").permitAll()
            // Auth
            .requestMatchers("/api/*/auth/**").permitAll()
            // Stream
            .requestMatchers("/api/*/rnd/chat/query/stream").permitAll()
            .requestMatchers("/api/*/rnd/chat/query/reject/stream").permitAll()
            .requestMatchers("/api/*/rnd/chat/query/proceed/stream").permitAll()
            .requestMatchers("/api/*/market/chat/query/stream").permitAll()
            // Swagger
            .requestMatchers("/swagger-ui/**", "/swagger-ui.html", "/swagger-ui/index.html").permitAll()
            .requestMatchers("/v3/api-docs/**", "/api-docs/**").permitAll()
            .requestMatchers("/swagger-resources/**").permitAll()
            .requestMatchers("/webjars/**").permitAll()
            .anyRequest().authenticated()
        );

        return http.build();
    }

    @Bean
    @Profile({"dev", "stage"})
    public SecurityFilterChain devSecurityFilterChain(HttpSecurity http) throws Exception {

        configureCommonBase(http);
        configureCommonHeaders(http);

        http.authorizeHttpRequests(auth -> auth
            // Default
            .requestMatchers("/favicon.ico").permitAll()
            // GCP LB
            .requestMatchers("/actuator/health").permitAll()
            .requestMatchers("/actuator/health/**").permitAll()
            // Auth
            .requestMatchers("/api/*/auth/**").permitAll()
            // Stream
            .requestMatchers("/api/*/rnd/chat/query/stream").permitAll()
            .requestMatchers("/api/*/rnd/chat/query/reject/stream").permitAll()
            .requestMatchers("/api/*/rnd/chat/query/proceed/stream").permitAll()
            .requestMatchers("/api/*/market/chat/query/stream").permitAll()
            // Swagger
            .requestMatchers("/swagger-ui/**", "/swagger-ui.html", "/swagger-ui/index.html").permitAll()
            .requestMatchers("/v3/api-docs/**", "/api-docs/**").permitAll()
            .requestMatchers("/swagger-resources/**").permitAll()
            .requestMatchers("/webjars/**").permitAll()
            .anyRequest().authenticated()
        );

        return http.build();
    }

    @Bean
    @Profile("prod")
    public SecurityFilterChain prodSecurityFilterChain(HttpSecurity http) throws Exception {

        configureCommonBase(http);
        configureCommonHeaders(http);

        http.authorizeHttpRequests(auth -> auth
            // Default
            .requestMatchers("/favicon.ico").permitAll()
            // GCP LB
            .requestMatchers("/actuator/health").permitAll()
            .requestMatchers("/actuator/health/**").permitAll()
            // Auth
            .requestMatchers("/api/*/auth/**").permitAll()
            // Stream
            .requestMatchers("/api/*/rnd/chat/query/stream").permitAll()
            .requestMatchers("/api/*/rnd/chat/query/reject/stream").permitAll()
            .requestMatchers("/api/*/rnd/chat/query/proceed/stream").permitAll()
            .requestMatchers("/api/*/market/chat/query/stream").permitAll()
            .anyRequest().authenticated()
        );

        return http.build();
    }

    @Bean
    public OAuth2UserService<OAuth2UserRequest, OAuth2User> customOAuth2UserService() {
        return new DefaultOAuth2UserService();
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return switch (passwordEncoderType) {
            case "argon2" -> Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();
            default -> new BCryptPasswordEncoder();
        };
    }

    @Bean
    public AuthenticationManager authenticationManager(AuthenticationConfiguration config) throws Exception {
        return config.getAuthenticationManager();
    }

    @Bean
    public CorsConfigurationSource corsConfigurationSource() {

        var configuration = new CorsConfiguration();
        configuration.setAllowedOrigins(allowedCorsOrigins);
        configuration.setAllowedMethods(List.of("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"));
        configuration.setAllowedHeaders(List.of("*"));
        configuration.setAllowCredentials(true);
        configuration.setMaxAge(3600L);

        var source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", configuration);
        return source;
    }

}
