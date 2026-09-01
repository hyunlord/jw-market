package com.jw.api.market.controller.v1;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

import com.jw.service.market.service.v1.MarketAgentService;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

/**
 * R24 STAGE 3-b — the streaming budget and the ceiling that keeps it below istio.
 *
 * <p>The budget was 120s, sized when a question reached two or three sources. With the full
 * fan-out restored, 12 of 15 measured turns ran past 120s (max 214s), so a finished answer
 * still reached the user as a truncation notice. These tests pin the new budget and the
 * ceiling, so raising either past istio's 300s has to break a test first.
 */
class MarketAgentControllerStreamBudgetTest {

    private MarketAgentController controller() {
        return new MarketAgentController(mock(MarketAgentService.class));
    }

    @Test
    void defaultStreamBudgetCoversTheMeasuredWorstTurn() {
        MarketAgentController controller = controller();

        long budget = (long) ReflectionTestUtils.getField(controller, "chatStreamTimeoutMillis");

        assertThat(budget).isEqualTo(Duration.ofSeconds(510).toMillis());
        // the slowest turn observed after the lane restore was 214s
        assertThat(budget).isGreaterThan(Duration.ofSeconds(214).toMillis());
        assertThat(MarketAgentController.timeoutNoticeDelayMillis(budget)).isEqualTo(509_000L);
    }

    @Test
    void ceilingClampsAboveSharedBudgetAndRecordsTheDecision() {
        List<String> records = new ArrayList<>();

        long budget = MarketAgentController.resolveChatStreamTimeout("600000", records::add);

        assertThat(budget).isEqualTo(510_000L);
        assertThat(records).singleElement().asString()
            .contains("clamped", "configured_ms=600000", "effective_ms=510000");
    }

    @Test
    void invalidBudgetsFallBackToDefaultAndRecordTheReason() {
        for (String configured : List.of("not-a-number", "0", "-1")) {
            List<String> records = new ArrayList<>();

            long budget = MarketAgentController.resolveChatStreamTimeout(configured, records::add);

            assertThat(budget).isEqualTo(510_000L);
            assertThat(records).singleElement().asString()
                .contains("fallback", "effective_ms=510000");
        }
    }

    @Test
    void configuredBudgetInsideTheBandIsAcceptedWithoutWarning() {
        List<String> records = new ArrayList<>();

        long budget = MarketAgentController.resolveChatStreamTimeout("2000", records::add);

        assertThat(budget).isEqualTo(2_000L);
        assertThat(records).isEmpty();
    }

    @Test
    void injectedTextValueBecomesTheEmitterBudgetAtStartup() {
        MarketAgentController controller = controller();
        ReflectionTestUtils.setField(controller, "configuredChatStreamTimeoutMillis", "2000");

        controller.configureChatStreamTimeout();

        assertThat((long) ReflectionTestUtils.getField(controller, "chatStreamTimeoutMillis"))
            .isEqualTo(2_000L);
    }
}
