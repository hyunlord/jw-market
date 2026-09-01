package com.jw.api.auth.controller.v1;

import com.jw.service.user.dto.v1.Auth;
import com.jw.service.user.service.v1.UserService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@Tag(name = "Auth API - v1", description = "권한 구성 API v1")
@RestController("AuthControllerV1")
@RequestMapping("/api/v1/auth")
public class AuthController {

    private final UserService userService;

    public AuthController(
        UserService userService
    ) {
        this.userService = userService;
    }

    @Operation(
        summary = "Login",
        description = "JW 사용자 로그인"
    )
    @PostMapping("/login")
    public ResponseEntity<Object> login(
        HttpServletRequest httpServletRequest,
        HttpServletResponse httpServletResponse,
        @RequestBody Auth.Request.Login authUserLogin
    ) { return ResponseEntity.ok(userService.getLogin(httpServletRequest, httpServletResponse, authUserLogin)); }

    @Operation(
        summary = "Login",
        description = "JW 사용자 로그인"
    )
    @PostMapping("/google/login")
    public ResponseEntity<Object> login(
        HttpServletRequest httpServletRequest,
        HttpServletResponse httpServletResponse,
        @RequestBody Auth.Request.Login.Google authUserLogin
    ) { return ResponseEntity.ok(userService.getLogin(httpServletRequest, httpServletResponse, authUserLogin)); }

    @Operation(
        summary = "Logout",
        description = "JW 사용자 로그아웃"
    )
    @GetMapping("/logout")
    public ResponseEntity<Auth.Response> logout(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(userService.logout(accessToken)); }

    @Operation(
        summary = "Verification",
        description = "JW 사용자 로그인 검증"
    )
    @GetMapping("/verification")
    public ResponseEntity<Auth.Response.Verification> verification(
        @RequestHeader("Authorization-Access-Token") String accessToken
    ) { return ResponseEntity.ok(userService.verification(accessToken)); }

    @Operation(
        summary = "GenOS Login",
        description = "GenOS 사용자 로그인"
    )
    @GetMapping("/genos/login")
    public ResponseEntity<Auth.Response> login() { return ResponseEntity.ok(userService.adminLogin()); }

    @Operation(
        summary = "GenOS Refresh",
        description = "GenOS Token Refresh"
    )
    @PostMapping("/genos/refresh")
    public ResponseEntity<Auth.Response> refresh(
        @RequestBody Auth.Request.Refresh refresh
    ) { return ResponseEntity.ok(userService.refresh(refresh)); }

}