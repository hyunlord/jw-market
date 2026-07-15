import json

from pipeline.scripts.etl import build_cache_cause as cause


def test_rows_for_channel_reuses_request_local_result() -> None:
    rows = [
        {
            "brand_name": "A",
            "channel_data": json.dumps({"의원": {"2026-01": {"raw_value": 3}}}),
        }
    ]
    cache: cause._ChannelRowsCache = {}

    first = cause._rows_for_channel(rows, "UBIST", "의원", ["2026-01"], channel_rows_cache=cache)
    second = cause._rows_for_channel(rows, "UBIST", "의원", ["2026-01"], channel_rows_cache=cache)

    assert first is second
    assert first[0]["metric_history"] == {"2026-01": 3.0}
    assert len(cache) == 1
