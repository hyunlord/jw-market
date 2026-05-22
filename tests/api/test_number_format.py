from __future__ import annotations

import json


def test_format_number_truncates_without_rounding():
    from pipeline.scripts.api.composers.number_format import format_number

    assert format_number(125479123456) == 125479123456
    assert format_number(12.34567) == 12.3456
    assert format_number(-6.90329) == -6.9032
    assert format_number(0.00009) == 0.0
    assert format_number(None) is None
    assert format_number(True) is True


def test_api_responses_do_not_include_display_unit_suffixes(client):
    market_status = client.get("/api/market-status")
    assert market_status.status_code == 200
    text = json.dumps(market_status.json(), ensure_ascii=False)
    assert "억" not in text
    assert "만원" not in text
    assert "조" not in text
