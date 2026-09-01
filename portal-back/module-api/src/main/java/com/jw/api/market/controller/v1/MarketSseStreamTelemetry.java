package com.jw.api.market.controller.v1;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;
import java.util.function.LongSupplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.codec.ServerSentEvent;

final class MarketSseStreamTelemetry {

    private static final Logger LOGGER = LoggerFactory.getLogger(MarketSseStreamTelemetry.class);
    private static final String ABSENT = "absent";

    private final String conversationId;
    private final LongSupplier nanoTime;
    private final Consumer<String> recordSink;
    private final long startedAtNanos;
    private final AtomicLong sentBytes = new AtomicLong();
    private final AtomicLong sentEvents = new AtomicLong();
    private final AtomicBoolean finished = new AtomicBoolean();
    private volatile String lastEvent = ABSENT;
    private volatile boolean bodySent;
    private volatile boolean frameLimitSeen;

    private MarketSseStreamTelemetry(
        String conversationId,
        LongSupplier nanoTime,
        Consumer<String> recordSink
    ) {
        this.conversationId = safeCorrelationId(conversationId);
        this.nanoTime = nanoTime;
        this.recordSink = recordSink;
        this.startedAtNanos = nanoTime.getAsLong();
    }

    static MarketSseStreamTelemetry start(String conversationId) {
        return new MarketSseStreamTelemetry(conversationId, System::nanoTime, LOGGER::info);
    }

    static MarketSseStreamTelemetry forTest(
        String conversationId,
        LongSupplier nanoTime,
        Consumer<String> recordSink
    ) {
        return new MarketSseStreamTelemetry(conversationId, nanoTime, recordSink);
    }

    void onSent(ServerSentEvent<String> event) {
        recordSentEvent(event, true);
    }

    void onNoticeSent(ServerSentEvent<String> event) {
        recordSentEvent(event, false);
    }

    private void recordSentEvent(ServerSentEvent<String> event, boolean answerEvent) {
        sentEvents.incrementAndGet();
        sentBytes.addAndGet(encodedBytes(event));
        lastEvent = safeToken(event.event());
        if (answerEvent && "markdown_block".equals(event.event())) {
            bodySent = true;
        }
        if (event.data() != null && event.data().contains("BFF_SSE_FRAME_LIMIT")) {
            frameLimitSeen = true;
        }
    }

    boolean bodySent() {
        return bodySent;
    }

    boolean frameLimitSeen() {
        return frameLimitSeen;
    }

    void finish(String requestedOutcome, boolean noticeSent) {
        if (!finished.compareAndSet(false, true)) {
            return;
        }
        String outcome = frameLimitSeen && "normal_complete".equals(requestedOutcome)
            ? "frame_limit"
            : safeToken(requestedOutcome);
        long elapsedMillis = Math.max(0L, (nanoTime.getAsLong() - startedAtNanos) / 1_000_000L);
        recordSink.accept(String.format(
            Locale.ROOT,
            "event=market_sse_stream_terminal outcome=%s elapsed_ms=%d sent_bytes=%d "
                + "sent_events=%d last_event=%s body_sent=%s conversation_id=%s notice_sent=%s",
            outcome,
            elapsedMillis,
            sentBytes.get(),
            sentEvents.get(),
            lastEvent,
            bodySent,
            conversationId,
            noticeSent
        ));
    }

    private static long encodedBytes(ServerSentEvent<String> event) {
        StringBuilder wire = new StringBuilder();
        if (event.id() != null) {
            wire.append("id:").append(event.id()).append('\n');
        }
        if (event.event() != null) {
            wire.append("event:").append(event.event()).append('\n');
        }
        if (event.retry() != null) {
            wire.append("retry:").append(event.retry().toMillis()).append('\n');
        }
        if (event.comment() != null) {
            wire.append(':').append(event.comment()).append('\n');
        }
        if (event.data() != null) {
            String[] lines = event.data().split("\\R", -1);
            for (String line : lines) {
                wire.append("data:").append(line).append('\n');
            }
        }
        wire.append('\n');
        return wire.toString().getBytes(StandardCharsets.UTF_8).length;
    }

    private static String safeCorrelationId(String value) {
        if (value == null || value.isBlank()) {
            return ABSENT;
        }
        return value.matches("[A-Za-z0-9_-]{1,128}") ? value : "invalid";
    }

    private static String safeToken(String value) {
        if (value == null || value.isBlank()) {
            return ABSENT;
        }
        return value.replaceAll("[^A-Za-z0-9_-]", "_");
    }
}
