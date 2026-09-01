package com.jw.api.market.controller.v1;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.springframework.web.servlet.mvc.method.annotation.ResponseBodyEmitter;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

class MarketAgentControllerTruncationNoticeTest {

    /** send/complete 만 관측하는 최소 스텁. 실패 주입을 위해 send 를 던지게 만들 수 있다. */
    private static final class RecordingEmitter extends SseEmitter {

        private final List<String> sent = new ArrayList<>();
        private final AtomicInteger completed = new AtomicInteger();
        private final AtomicInteger attempts = new AtomicInteger();
        private final boolean failOnSend;

        private RecordingEmitter(boolean failOnSend) {
            this.failOnSend = failOnSend;
        }

        @Override
        public void send(SseEventBuilder builder) throws IOException {
            attempts.incrementAndGet();
            if (failOnSend) {
                throw new IOException("simulated downstream disconnect");
            }
            for (ResponseBodyEmitter.DataWithMediaType part : builder.build()) {
                sent.add(String.valueOf(part.getData()));
            }
        }

        @Override
        public void complete() {
            completed.incrementAndGet();
        }
    }

    @Test
    void truncationSendsTheNoticeBeforeClosingTheStream() {
        RecordingEmitter emitter = new RecordingEmitter(false);

        MarketAgentController.completeWithTruncationNotice(
            emitter, MarketSseStreamTelemetry.forTest("conversation", System::nanoTime, ignored -> { }));

        String wire = String.join("", emitter.sent);
        assertThat(wire).contains("markdown_block", "시간 안에 끝나지 않아", "error", "done");
        assertThat(emitter.completed).hasValue(1);
    }

    @Test
    void noticeIsAppendedBeforeTheTerminalEvents() {
        RecordingEmitter emitter = new RecordingEmitter(false);

        MarketAgentController.completeWithTruncationNotice(
            emitter, MarketSseStreamTelemetry.forTest("conversation", System::nanoTime, ignored -> { }));

        String wire = String.join("", emitter.sent);
        assertThat(wire.indexOf("markdown_block")).isLessThan(wire.indexOf("error"));
        assertThat(wire.indexOf("error")).isLessThan(wire.lastIndexOf("done"));
    }

    @Test
    void timeoutAfterAnAnswerBodyUsesThePartialResultNotice() {
        RecordingEmitter emitter = new RecordingEmitter(false);
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.forTest(
            "conversation", System::nanoTime, ignored -> { });
        telemetry.onSent(org.springframework.http.codec.ServerSentEvent.<String>builder()
            .event("markdown_block")
            .data("{\"markdown\":\"partial answer\"}")
            .build());

        MarketAgentController.completeWithTruncationNotice(emitter, telemetry);

        assertThat(String.join("", emitter.sent))
            .contains("여기까지 표시합니다")
            .doesNotContain("결과를 표시하지 못했습니다");
    }

    /**
     * 실패 주입 — 고지 전송이 실패해도 스트림은 반드시 마감돼야 한다.
     * 고지가 답변을 죽이면 원래 문제보다 나빠진다.
     */
    @Test
    void noticeFailureStillClosesTheStream() {
        RecordingEmitter emitter = new RecordingEmitter(true);
        List<String> records = new ArrayList<>();
        MarketSseStreamTelemetry telemetry = MarketSseStreamTelemetry.forTest(
            "conversation", System::nanoTime, records::add);

        MarketAgentController.completeWithTruncationNotice(emitter, telemetry);

        assertThat(emitter.sent).isEmpty();
        assertThat(emitter.attempts).hasValue(1);
        assertThat(emitter.completed).hasValue(1);
        assertThat(records).singleElement().asString()
            .contains("outcome=timeout", "notice_sent=false");
    }
}
