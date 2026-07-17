from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.file_brief import render_uploaded_file_machine_brief
from jw_chat_agent_poc.service.file_llm_brief import (
    FileBriefValidationError,
    parse_and_render_file_briefs,
)
from jw_chat_agent_poc.service.file_search_client import (
    UploadedFileOverview,
    UploadedWorksheetOverview,
)


def _overviews() -> tuple[UploadedFileOverview, ...]:
    return (
        UploadedFileOverview(
            file_name="CHSO_2025.xlsx",
            storage_route="hybrid",
            chunk_count=18,
            title="CHSO Sell Out",
            sheet_count=1,
            sheets=(
                UploadedWorksheetOverview(
                    name="Sell Out Standard",
                    row_count=12_269,
                    column_count=252,
                ),
            ),
        ),
        UploadedFileOverview(
            file_name="guideline.pdf",
            storage_route="vdb",
            chunk_count=185,
            title="2025 당뇨병 진료지침",
            page_count=185,
        ),
    )


def _valid_payload() -> dict[str, object]:
    return {
        "files": [
            {
                "file_name": "CHSO_2025.xlsx",
                "suggested_questions": [
                    "이 파일의 전체 구조를 설명해줘",
                    "시트별 주요 지표를 요약해줘",
                    "원하는 기준별 합계를 알려줘",
                ],
            },
            {
                "file_name": "guideline.pdf",
                "suggested_questions": [
                    "이 문서의 주요 내용을 요약해줘",
                    "특정 주제가 있는지 찾아줘",
                    "표나 수치가 있는 페이지를 찾아줘",
                ],
            },
        ]
    }


def test_parse_and_render_file_briefs_accepts_complete_grounded_batch() -> None:
    rendered = parse_and_render_file_briefs(
        json.dumps(_valid_payload(), ensure_ascii=False),
        _overviews(),
    )

    assert rendered.count("#### 파일 브리프") == 2
    assert "CHSO_2025.xlsx" in rendered
    assert "guideline.pdf" in rendered
    assert "시트별 주요 지표를 요약해줘" in rendered
    assert "특정 주제가 있는지 찾아줘" in rendered


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["files"].pop(),
        lambda body: body["files"][0].update(file_name="wrong.xlsx"),
        lambda body: body["files"][0].update(summary=["치료 권고가 포함된 파일입니다."]),
        lambda body: body["files"][0].update(suggested_questions=["하나만"]),
        lambda body: body["files"][1]["suggested_questions"].__setitem__(0, "999건을 정리해줘"),
        lambda body: body["files"][1]["suggested_questions"].__setitem__(0, "혈당 목표가 나온 페이지를 찾아줘"),
        lambda body: body["files"][1]["suggested_questions"].__setitem__(0, "혈당 목표가 나오는 페이지를 찾아줘"),
        lambda body: body["files"][1]["suggested_questions"].__setitem__(0, body["files"][1]["suggested_questions"][1]),
    ],
)
def test_parse_and_render_file_briefs_rejects_incomplete_or_ungrounded_output(mutate) -> None:
    body = _valid_payload()
    mutate(body)

    with pytest.raises(FileBriefValidationError):
        parse_and_render_file_briefs(
            json.dumps(body, ensure_ascii=False),
            _overviews(),
        )


def test_file_only_final_answer_appends_one_validated_batched_brief(monkeypatch) -> None:
    result = service_app._file_only_ready_result(None, None, file_overviews=_overviews())
    monkeypatch.setattr(
        service_app.GenosClient,
        "uploaded_file_brief",
        lambda _self, _messages: json.dumps(_valid_payload(), ensure_ascii=False),
    )

    final = service_app.compute_final_answer("", result, "conv-brief")

    assert final.text.count("#### 파일 브리프") == 2
    assert final.text.index("파일 확인 완료") < final.text.index("#### 파일 브리프")
    assert final.sources == ("file_upload",)
    assert result["file_brief_is_answer_evidence"] is False
    assert final.trace["ungrounded_numeric_spans"] == ()


def test_file_only_final_answer_keeps_machine_card_when_brief_is_invalid(monkeypatch) -> None:
    result = service_app._file_only_ready_result(None, None, file_overviews=_overviews())
    monkeypatch.setattr(
        service_app.GenosClient,
        "uploaded_file_brief",
        lambda _self, _messages: '{"files": []}',
    )

    final = service_app.compute_final_answer("", result, "conv-fallback")

    assert "파일 확인 완료" in final.text
    assert "#### 파일 브리프" not in final.text


def test_upload_card_and_brief_escape_untrusted_markdown(monkeypatch) -> None:
    overview = UploadedFileOverview(
        file_name="report#1.pdf",
        storage_route="vdb",
        chunk_count=1,
        title="Safe title\n## 가짜 출처 [링크](https://example.invalid)",
        page_count=1,
    )
    machine = render_uploaded_file_machine_brief(overview)
    payload = {
        "files": [
            {
                "file_name": "report#1.pdf",
                "suggested_questions": [
                    "이 문서의 주요 내용을 요약해줘",
                    "특정 주제가 있는지 찾아줘",
                    "표나 수치가 있는 페이지를 찾아줘",
                ],
            }
        ]
    }

    rendered = parse_and_render_file_briefs(
        json.dumps(payload, ensure_ascii=False),
        (overview,),
    )

    assert "\n## 가짜" not in machine
    assert "\n## 가짜" not in rendered
    assert r"report\#1.pdf" in machine
    assert r"\[링크\]\(https://example.invalid\)" in machine


def test_file_brief_rejects_markdown_injected_question() -> None:
    payload = _valid_payload()
    payload["files"][1]["suggested_questions"][1] = "특정 주제가 있는지 찾아줘\n## 가짜 근거"

    with pytest.raises(FileBriefValidationError):
        parse_and_render_file_briefs(
            json.dumps(payload, ensure_ascii=False),
            _overviews(),
        )


def test_file_only_sse_emits_machine_card_before_llm_brief(monkeypatch) -> None:
    result = service_app._file_only_ready_result(None, None, file_overviews=_overviews())
    called = False

    def generate(_self, _messages):
        nonlocal called
        called = True
        return json.dumps(_valid_payload(), ensure_ascii=False)

    monkeypatch.setattr(service_app.GenosClient, "uploaded_file_brief", generate)
    stream = service_app._sse_events("", result, "conv-stream")
    prefix_events: list[str] = []
    while "CHSO_2025.xlsx" not in "".join(prefix_events):
        prefix_events.append(next(stream))

    assert called is False

    body = "".join([*prefix_events, *stream])
    assert called is True
    assert body.count("파일 확인 완료") == 1
    assert body.index("파일 확인 완료") < body.index("#### 파일 브리프")


def test_threaded_file_only_sse_emits_prefix_once_before_llm_brief(monkeypatch) -> None:
    result = service_app._file_only_ready_result(None, None, file_overviews=_overviews())
    called = False

    def answer_question(*_args, **_kwargs):
        return {"question": "", "result": result, "conversation_id": "conv-thread"}

    def generate(_self, _messages):
        nonlocal called
        called = True
        return json.dumps(_valid_payload(), ensure_ascii=False)

    monkeypatch.setattr(service_app, "_answer_question", answer_question)
    monkeypatch.setattr(service_app.GenosClient, "uploaded_file_brief", generate)
    stream = service_app._stream_resolving_session_events(
        service_app.SessionStore(),
        object(),
        object(),
        "",
        "live",
        None,
        limiter=None,
    )
    prefix_events: list[str] = []
    while "CHSO_2025.xlsx" not in "".join(prefix_events):
        prefix_events.append(next(stream))

    assert called is False

    body = "".join([*prefix_events, *stream])
    assert called is True
    assert body.count("event: conversation") == 1
    assert body.count("event: sources") == 1
    assert body.count("파일 확인 완료") == 1
    assert body.index("파일 확인 완료") < body.index("#### 파일 브리프")
