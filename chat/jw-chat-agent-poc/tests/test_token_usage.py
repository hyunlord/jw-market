from jw_chat_agent_poc.common.token_usage import (
    public_token_usage,
    record_token_usage,
    usage_call_from_payload,
)


def test_usage_call_accepts_genos_camel_case_usage_metadata() -> None:
    call = usage_call_from_payload(
        {
            "model": "gemini-test",
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
                "totalTokenCount": 18,
            },
        },
        base_url="https://example.test/serving/163/v1",
        stream=True,
    )

    assert call is not None
    assert call["input_tokens"] == 11
    assert call["output_tokens"] == 7
    assert call["total_tokens"] == 18
    assert call["serving_id"] == "163"

    timing: dict = {}
    record_token_usage(timing, call)

    assert public_token_usage(timing) == {
        "available": True,
        "calls": [call],
        "total_input_tokens": 11,
        "total_output_tokens": 7,
        "total_tokens": 18,
    }
