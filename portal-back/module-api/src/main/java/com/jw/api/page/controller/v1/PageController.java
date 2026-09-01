package com.jw.api.page.controller.v1;

import com.jw.core.base.entity.UserAccess;
import com.jw.core.base.response.Response;
import com.jw.core.config.annotation.AccessUser;
import com.jw.service.page.service.v1.PageService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@Tag(name = "Page API - v1", description = "PAGE 구성 API v1")
@RestController("PageControllerV1")
@RequestMapping("/api/v1/page")
public class PageController {

    private final PageService pageService;

    public PageController(
            @Qualifier("PageServiceV1") PageService pageService
    ) {
        this.pageService = pageService;
    }

    @Operation(
        summary = "Page 권한 조회",
        description = "Page 권한 조회"
    )
    @GetMapping("")
    public ResponseEntity<Response> brands(
        @AccessUser UserAccess userAccess,
        @RequestParam String pageUrl
    ) { return ResponseEntity.ok(pageService.getPage(userAccess, pageUrl)); }
}
