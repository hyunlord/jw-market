package com.jw.api.market.controller.v1;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jw.service.market.dto.v1.Market;
import com.jw.service.market.service.v1.MarketAgentService;
import com.jw.service.market.service.v1.MarketSseNotice;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.test.util.ReflectionTestUtils;
import reactor.core.Disposable;
import reactor.core.publisher.Flux;

class MarketAgentControllerDeadlineTest {

    @Test
    void applicationDeadlineSendsNoticeBeforeSpringCompletesTheEmitter() throws Exception {
        MarketAgentService service = serviceReturning(Flux.never());
        CapturingDeadlineScheduler scheduler = new CapturingDeadlineScheduler();
        MarketAgentController controller = new MarketAgentController(service, scheduler);
        ReflectionTestUtils.setField(controller, "chatStreamTimeoutMillis", 2_000L);
        MockMvc mockMvc = MockMvcBuilders.standaloneSetup(controller).build();

        MvcResult result = performStream(mockMvc);

        assertThat(result.getRequest().getAsyncContext().getTimeout()).isEqualTo(2_000L);
        assertThat(scheduler.delayMillis).isEqualTo(1_000L);
        scheduler.runDeadline();
        mockMvc.perform(asyncDispatch(result)).andExpect(status().isOk());

        String body = result.getResponse().getContentAsString();
        assertThat(body)
            .containsSubsequence(
                "event:markdown_block",
                "event:error",
                "BFF_SSE_STREAM_TIMEOUT",
                "event:done");
    }

    @Test
    void normalCompletionCancelsDeadlineWithoutChangingTheNormalFrames() throws Exception {
        MarketAgentService service = serviceReturning(Flux.just(
            ServerSentEvent.<String>builder().event("step").data("working").build(),
            ServerSentEvent.<String>builder().event("done").data("complete").build()));
        CapturingDeadlineScheduler scheduler = new CapturingDeadlineScheduler();
        MarketAgentController controller = new MarketAgentController(service, scheduler);
        ReflectionTestUtils.setField(controller, "chatStreamTimeoutMillis", 2_000L);
        MockMvc mockMvc = MockMvcBuilders.standaloneSetup(controller).build();

        MvcResult result = performStream(mockMvc);
        mockMvc.perform(asyncDispatch(result)).andExpect(status().isOk());

        assertThat(scheduler.disposed).isTrue();
        assertThat(result.getResponse().getContentAsString())
            .containsSubsequence("step", "working", "done", "complete")
            .doesNotContain("BFF_SSE_STREAM_TIMEOUT");

        scheduler.runDeadline();
        assertThat(result.getResponse().getContentAsString())
            .doesNotContain("BFF_SSE_STREAM_TIMEOUT");
    }

    @Test
    void deadlinePreservesAnAlreadySentBodyBeforeTheTerminalNotice() throws Exception {
        MarketAgentService service = serviceReturning(Flux.concat(
            Flux.just(ServerSentEvent.<String>builder()
                .event("markdown_block")
                .data("partial answer")
                .build()),
            Flux.never()));
        CapturingDeadlineScheduler scheduler = new CapturingDeadlineScheduler();
        MarketAgentController controller = new MarketAgentController(service, scheduler);
        ReflectionTestUtils.setField(controller, "chatStreamTimeoutMillis", 2_000L);
        MockMvc mockMvc = MockMvcBuilders.standaloneSetup(controller).build();

        MvcResult result = performStream(mockMvc);
        scheduler.runDeadline();
        mockMvc.perform(asyncDispatch(result)).andExpect(status().isOk());

        assertThat(result.getResponse().getContentAsString())
            .containsSubsequence(
                "partial answer",
                "event:error",
                "BFF_SSE_STREAM_TIMEOUT",
                "event:done");
    }

    @Test
    void upstreamFailureCancelsTheDeadlineBeforeCompletingWithError() {
        MarketAgentService service = serviceReturning(Flux.error(new IllegalStateException("upstream")));
        CapturingDeadlineScheduler scheduler = new CapturingDeadlineScheduler();
        MarketAgentController controller = new MarketAgentController(service, scheduler);
        ReflectionTestUtils.setField(controller, "chatStreamTimeoutMillis", 2_000L);

        controller.chatQueryStream("portal-token", mock(Market.Request.Chat.Stream.class));

        assertThat(scheduler.disposed).isTrue();
        scheduler.runDeadline();
        assertThat(scheduler.runs).isZero();
    }

    @Test
    void frameLimitNoticeWinsWhenItsTerminalCompletionMeetsTheDeadline() throws Exception {
        MarketAgentService service = serviceReturning(Flux.concat(
            Flux.fromIterable(MarketSseNotice.frameLimitEvents()),
            Flux.never()));
        CapturingDeadlineScheduler scheduler = new CapturingDeadlineScheduler();
        MarketAgentController controller = new MarketAgentController(service, scheduler);
        ReflectionTestUtils.setField(controller, "chatStreamTimeoutMillis", 2_000L);
        MockMvc mockMvc = MockMvcBuilders.standaloneSetup(controller).build();

        MvcResult result = performStream(mockMvc);
        scheduler.runDeadline();
        mockMvc.perform(asyncDispatch(result)).andExpect(status().isOk());

        assertThat(result.getResponse().getContentAsString())
            .contains("BFF_SSE_FRAME_LIMIT")
            .doesNotContain("BFF_SSE_STREAM_TIMEOUT");
    }

    private static MarketAgentService serviceReturning(Flux<ServerSentEvent<String>> stream) {
        MarketAgentService service = mock(MarketAgentService.class);
        when(service.getChatAppQueryStream(eq("portal-token"), any())).thenReturn(stream);
        return service;
    }

    private static MvcResult performStream(MockMvc mockMvc) throws Exception {
        return mockMvc.perform(post("/api/v1/market/chat/query/stream")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"question\":\"question\",\"conversationId\":\"conversation\"}"))
            .andExpect(request().asyncStarted())
            .andReturn();
    }

    private static final class CapturingDeadlineScheduler implements MarketSseDeadlineScheduler {

        private Runnable task;
        private long delayMillis = -1L;
        private boolean disposed;
        private int runs;

        @Override
        public Disposable schedule(Runnable deadlineTask, long deadlineDelayMillis) {
            task = deadlineTask;
            delayMillis = deadlineDelayMillis;
            return () -> disposed = true;
        }

        private void runDeadline() {
            if (!disposed) {
                runs++;
                task.run();
            }
        }
    }

}
