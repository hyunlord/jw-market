from datetime import datetime

from bundle_builder import build_brand_bundle, render_narrative
from bundle_builder.prompt_renderer import format_percent

from .conftest import KST


def _target_metric_rows(narrative: str):
    lines = narrative.splitlines()
    for idx, line in enumerate(lines):
        if line == "| 월 | Raw value | M/S | 순위 | MoM | YoY | MAT YoY |":
            cursor = idx + 2
            while cursor < len(lines) and lines[cursor].startswith("| ") and lines[cursor].count("|") >= 7:
                yield lines[cursor]
                cursor += 1


def _competitor_ms_rows(narrative: str):
    lines = narrative.splitlines()
    headers = {
        "| 순위 | Brand | Raw value | M/S | EI | CAGR 5y | Momentum |": 4,
        "| 순위 | Brand | Raw value | M/S |": 4,
    }
    for idx, line in enumerate(lines):
        if line in headers:
            ms_index = headers[line]
            cursor = idx + 2
            while cursor < len(lines) and lines[cursor].startswith("| ") and lines[cursor].count("|") >= ms_index + 1:
                yield lines[cursor], ms_index
                cursor += 1


def test_format_percent_ratio_kind():
    assert format_percent(8.90, kind="ratio") == "8.90%"
    assert format_percent(0.0, kind="ratio") == "0.00%"
    assert format_percent(None, kind="ratio") == "N/A"
    assert format_percent(-1.0, kind="ratio") == "-1.00%"


def test_format_percent_change_kind():
    assert format_percent(34.87, kind="change") == "+34.87%"
    assert format_percent(-21.44, kind="change") == "-21.44%"
    assert format_percent(0.0, kind="change") == "+0.00%"
    assert format_percent(None, kind="change") == "N/A"


def test_ms_no_sign_in_narrative(db_conn, config_v1_1):
    bundle = build_brand_bundle("가드메트", datetime(2026, 5, 25, 8, 0, tzinfo=KST), config_v1_1, db_conn)
    narrative = render_narrative(bundle)

    checked = 0
    for line in _target_metric_rows(narrative):
        cols = [col.strip() for col in line.split("|")]
        ms_col = cols[3]
        assert not ms_col.startswith("+"), f"M/S has + sign: {ms_col!r} in line: {line}"
        checked += 1

    for line, ms_index in _competitor_ms_rows(narrative):
        cols = [col.strip() for col in line.split("|")]
        ms_col = cols[ms_index]
        if ms_col != "-":
            assert not ms_col.startswith("+"), f"M/S has + sign: {ms_col!r} in line: {line}"
        checked += 1

    assert checked > 0


def test_change_metrics_have_sign(db_conn, config_v1_1):
    bundle = build_brand_bundle("가드메트", datetime(2026, 5, 25, 8, 0, tzinfo=KST), config_v1_1, db_conn)
    narrative = render_narrative(bundle)
    signed_changes = []
    for line in _target_metric_rows(narrative):
        cols = [col.strip() for col in line.split("|")]
        signed_changes.extend([cols[5], cols[6], cols[7]])
    assert any(value.startswith("+") for value in signed_changes if value != "-")
    assert any(value.startswith("-") for value in signed_changes if value != "-")
