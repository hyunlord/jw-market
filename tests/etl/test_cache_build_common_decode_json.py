import math

import pytest

from pipeline.scripts.etl import cache_build_common


def test_decode_json_uses_orjson_for_valid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("orjson")

    def reject_standard_decoder(_value: str) -> None:
        raise AssertionError("valid JSON should use the installed fast decoder")

    monkeypatch.setattr(cache_build_common.json, "loads", reject_standard_decoder)

    assert cache_build_common.decode_json('{"period":"2026-05","value":1.25}') == {
        "period": "2026-05",
        "value": 1.25,
    }


def test_decode_json_preserves_standard_decoder_fallback() -> None:
    pytest.importorskip("orjson")

    decoded = cache_build_common.decode_json('{"value":NaN}')

    assert math.isnan(decoded["value"])
