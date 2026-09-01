package com.jw.api.market.controller.v1;

import com.jw.api.config.handler.GlobalExceptionHandler;
import com.jw.service.market.dto.v1.Market;
import com.jw.service.market.service.v1.MarketAgentService;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.Test;
import org.springframework.core.env.StandardEnvironment;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.slf4j.LoggerFactory;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class MarketAgentControllerMethodContractTest {

    @Test
    void documentGetIs405WithAllowAndTelemetry() throws Exception {
        MockMvc mvc = mvc();
        Logger logger = (Logger) LoggerFactory.getLogger(
            GlobalExceptionHandler.class
        );
        Level previousLevel = logger.getLevel();
        logger.setLevel(Level.WARN);
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start();
        logger.addAppender(appender);

        try {
            mvc.perform(get("/api/v1/market/chat/document")
                    .header("Authorization-Access-Token", "test-token")
                    .header("User-Agent", "legacy-portal")
                    .header("X-Portal-Bundle", "index-old.js"))
                .andExpect(status().isMethodNotAllowed())
                .andExpect(header().string("Allow", "POST"));
        } finally {
            logger.detachAppender(appender);
            logger.setLevel(previousLevel);
            appender.stop();
        }

        assertThat(appender.list).hasSize(1);
        assertThat(appender.list.getFirst().getFormattedMessage())
            .contains("portal_method_not_allowed")
            .contains("method=GET")
            .contains("path=/api/v1/market/chat/document")
            .contains("user_agent=legacy-portal")
            .contains("portal_bundle=index-old.js")
            .contains("trace_id=");
    }

    @Test
    void unregisteredPathRemains404RatherThanBecoming500() throws Exception {
        mvc().perform(get("/api/v1/market/chat/not-registered"))
            .andExpect(status().isNotFound());
    }

    @Test
    void uploadStatusRoutePreservesSessionOwnershipInputs() throws Exception {
        MarketAgentService service = mock(MarketAgentService.class);
        when(service.getDocumentsUploadStatus(any(), any())).thenReturn(null);
        MockMvc mvc = mvc(service);

        mvc.perform(get("/api/v1/market/chat/document/upload/status")
                .header("Authorization-Access-Token", "portal-token")
                .queryParam("workflow_id", "301")
                .queryParam("app_session_id", "session-123")
                .queryParam("upload_id", "upload-123"))
            .andExpect(status().isOk());

        var request = org.mockito.ArgumentCaptor.forClass(
            Market.Request.Document.UploadStatus.class
        );
        verify(service).getDocumentsUploadStatus(
            org.mockito.ArgumentMatchers.eq("portal-token"),
            request.capture()
        );
        assertThat(request.getValue().getWorkflow_id()).isEqualTo(301L);
        assertThat(request.getValue().getApp_session_id())
            .isEqualTo("session-123");
        assertThat(request.getValue().getUpload_id()).isEqualTo("upload-123");
    }

    @Test
    void lazyDetailRoutePreservesStableLookupInputs() throws Exception {
        MarketAgentService service = mock(MarketAgentService.class);
        when(service.getChatDetail(any(), any(), any(), any())).thenReturn(null);
        MockMvc mvc = mvc(service);

        mvc.perform(get("/api/v1/market/chat/detail/conversation-123/response-456")
                .header("Authorization-Access-Token", "portal-token")
                .queryParam("item_key", "inspection:7"))
            .andExpect(status().isOk());

        verify(service).getChatDetail(
            "portal-token",
            "conversation-123",
            "response-456",
            "inspection:7"
        );
    }

    private static MockMvc mvc() {
        return mvc(mock(MarketAgentService.class));
    }

    private static MockMvc mvc(MarketAgentService service) {
        MarketAgentController controller = new MarketAgentController(service);
        return MockMvcBuilders.standaloneSetup(controller)
            .setControllerAdvice(
                new GlobalExceptionHandler(new StandardEnvironment())
            )
            .build();
    }
}
