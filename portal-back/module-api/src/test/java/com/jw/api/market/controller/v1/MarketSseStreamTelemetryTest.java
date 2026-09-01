package com.jw.api.market.controller.v1;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;

class MarketSseStreamTelemetryTest {

    @Test
    void normalAndTimeoutOutcomesUseTheSameStructuredSchema() {
        AtomicLong now = new AtomicLong(1_000_000_000L);
        List<String> records = new ArrayList<>();
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.forTest(
            "conversation-1", now::get, records::add);

        telemetry.onSent(ServerSentEvent.<String>builder()
            .event("markdown_block")
            .data("{\"markdown\":\"answer\"}")
            .build());
        now.set(3_000_000_000L);
        telemetry.finish("timeout", true);

        assertThat(records).singleElement().asString()
            .contains(
                "event=market_sse_stream_terminal",
                "outcome=timeout",
                "elapsed_ms=2000",
                "sent_bytes=",
                "sent_events=1",
                "last_event=markdown_block",
                "body_sent=true",
                "conversation_id=conversation-1",
                "notice_sent=true");
    }

    @Test
    void terminalOutcomeIsRecordedOnlyOnce() {
        List<String> records = new ArrayList<>();
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.forTest(
            "conversation-2", () -> 1_000_000_000L, records::add);

        telemetry.finish("normal_complete", false);
        telemetry.finish("downstream_error", false);

        assertThat(records).hasSize(1);
        assertThat(records.get(0)).contains("outcome=normal_complete");
    }

    @Test
    void timeoutNoticeDoesNotMasqueradeAsAnAnswerBody() {
        List<String> records = new ArrayList<>();
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.forTest(
            "conversation-3", () -> 1_000_000_000L, records::add);

        telemetry.onNoticeSent(ServerSentEvent.<String>builder()
            .event("markdown_block")
            .data("{\"markdown\":\"notice\"}")
            .build());
        telemetry.finish("timeout", true);

        assertThat(records).singleElement().asString()
            .contains("body_sent=false", "notice_sent=true");
    }

    @Test
    void correlationIdIsSanitizedBeforeLogging() {
        List<String> records = new ArrayList<>();
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.forTest(
            "conversation secret\nAuthorization: bearer", () -> 1_000_000_000L, records::add);

        telemetry.finish("normal_complete", false);

        assertThat(records).singleElement().asString()
            .contains("conversation_id=invalid")
            .doesNotContain("secret", "Authorization", "bearer");
    }
}
