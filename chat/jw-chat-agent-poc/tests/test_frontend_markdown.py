from __future__ import annotations

from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def test_frontend_renders_streamed_markdown_with_sanitization() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert "marked" in html
    assert "DOMPurify.sanitize" in html
    assert "marked.parse" in html
    assert "target.innerHTML" in html
    assert "answer.textContent += event.data" not in html


def test_frontend_renders_charts_as_optional_layer() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert "chart.js" in html
    assert 'source.addEventListener("charts"' in html
    assert "function renderCharts" in html
    assert "if (!window.Chart)" in html
    assert "rawAnswer += event.data" in html
    assert 'const externalMode = queryParams.get("external_mode") || "live"' in html
    assert "external_mode=${encodeURIComponent(externalMode)}" in html


def test_frontend_sends_conversation_id_and_keeps_transcript() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert "conversationId" in html
    assert "conversation_id=${encodeURIComponent(conversationId)}" in html
    assert 'source.addEventListener("conversation"' in html
    assert 'id="transcript"' in html
    assert "appendMessage" in html


def test_frontend_streams_into_one_answer_target_and_blocks_reconnect_duplicates() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert "let streamSequence = 0;" in html
    assert "const streamId = streamSequence;" in html
    assert "function isActiveStream(source, streamId)" in html
    assert "streamCompleted = true;" in html
    assert "source.close();" in html
    assert "renderActiveAnswer(rawAnswer);" in html
    assert "answer.hidden = true;" in html
    assert "renderMarkdown(rawAnswer);" not in html


def test_frontend_keeps_composer_fixed_at_bottom_with_scrollable_chat() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert "min-height: 100dvh" in html
    assert "height: 100dvh" in html
    assert "grid-template-rows: auto minmax(0, 1fr) auto" in html
    assert 'class="chat-panel"' in html
    assert "overflow-y: auto" in html
    assert 'class="composer"' in html
    assert "position: sticky" in html
    assert "bottom: 0" in html
    assert "scrollConversationToBottom" in html
    assert "chatPanel.scrollTo" in html
