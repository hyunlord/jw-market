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
import com.jw.service.market.service.v1.MarketSseTimingProbe;
import java.lang.reflect.Method;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

class MarketAgentControllerSseEmitterTest {

    @Test
    void streamEndpointRequiresTheServletSseEmitterContract() throws Exception {
        Method method = MarketAgentController.class.getMethod(
            "chatQueryStream", String.class, Market.Request.Chat.Stream.class);

        assertThat(method.getReturnType()).isEqualTo(SseEmitter.class);
    }

    @Test
    void forwardsTheFirstNamedEventBeforeTheUpstreamCompletes() throws Exception {
        MarketAgentService service = mock(MarketAgentService.class);
        when(service.getChatAppQueryStream(eq("portal-token"), any()))
            .thenReturn(Flux.concat(
                Flux.just(ServerSentEvent.<String>builder()
                    .event("step")
                    .data("{\"name\":\"lookup-start\"}")
                    .build()),
                Mono.delay(Duration.ofMillis(500))
                    .map(ignored -> ServerSentEvent.<String>builder()
                        .event("done")
                        .data("{\"status\":\"ok\"}")
                        .build())
            ));
        MockMvc mockMvc = MockMvcBuilders
            .standaloneSetup(new MarketAgentController(service))
            .build();

        MvcResult result = mockMvc.perform(post("/api/v1/market/chat/query/stream")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"question\":\"리바로 최신 매출 알려줘\",\"conversationId\":\"conversation\"}"))
            .andExpect(request().asyncStarted())
            .andReturn();

        String earlyBody = waitForBody(result, "event:step", Duration.ofMillis(300));
        assertThat(earlyBody).contains("event:step", "data:{\"name\":\"lookup-start\"}");
        assertThat(earlyBody).doesNotContain("event:done");

        mockMvc.perform(asyncDispatch(result)).andExpect(status().isOk());
        assertThat(result.getResponse().getContentAsString())
            .containsSubsequence(
                "event:step",
                "data:{\"name\":\"lookup-start\"}",
                "event:done",
                "data:{\"status\":\"ok\"}"
            );
    }

    @Test
    void enabledInstrumentationKeepsTheNamedFrameContract() throws Exception {
        MarketAgentService service = mock(MarketAgentService.class);
        when(service.getChatAppQueryStream(eq("portal-token"), any(), any(MarketSseTimingProbe.class)))
            .thenReturn(Flux.just(
                ServerSentEvent.<String>builder().event("step").data("{\"name\":\"lookup\"}").build(),
                ServerSentEvent.<String>builder().event("done").data("{\"status\":\"ok\"}").build()
            ));
        MarketAgentController controller = new MarketAgentController(service);
        ReflectionTestUtils.setField(controller, "sseTimingEnabled", true);
        MockMvc mockMvc = MockMvcBuilders.standaloneSetup(controller).build();

        MvcResult result = mockMvc.perform(post("/api/v1/market/chat/query/stream")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"question\":\"question\",\"conversationId\":\"conversation\"}"))
            .andExpect(request().asyncStarted())
            .andReturn();

        mockMvc.perform(asyncDispatch(result)).andExpect(status().isOk());
        assertThat(result.getResponse().getContentAsString())
            .containsSubsequence("event:step", "event:done");
    }

    private static String waitForBody(MvcResult result, String expected, Duration timeout)
        throws Exception {
        long deadline = System.nanoTime() + timeout.toNanos();
        String body;
        do {
            body = result.getResponse().getContentAsString();
            if (body.contains(expected)) {
                return body;
            }
            Thread.sleep(10);
        } while (System.nanoTime() < deadline);
        return body;
    }
}
