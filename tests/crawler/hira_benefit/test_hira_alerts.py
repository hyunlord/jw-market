from __future__ import annotations

import json
from typing import Self

from pipeline.scripts.crawler.hira_benefit.alerts import (
    AlertEvent,
    evaluate_failed_ratio,
    publish_alert,
)


def test_recent_n_failed_ratio_uses_aggregate_counts() -> None:
    evaluation = evaluate_failed_ratio(
        ((100, 1), (50, 4), (50, 5)),
        threshold=0.04,
    )

    assert evaluation.runs == 3
    assert evaluation.parsed_count == 200
    assert evaluation.failed_count == 10
    assert evaluation.ratio == 0.05
    assert evaluation.triggered is True


def test_alert_delivery_is_disabled_without_configured_channel() -> None:
    result = publish_alert(
        AlertEvent(
            event="hira_parse_failed_ratio",
            run_id="run-1",
            failed_count=10,
            parsed_count=200,
            failed_ratio=0.05,
            threshold=0.04,
            window_runs=3,
        ),
        endpoint=None,
    )

    assert result.status == "disabled"
    assert result.attempts == 0


def test_alert_webhook_payload_is_json_and_delivery_is_best_effort() -> None:
    bodies: list[dict[str, object]] = []

    class Response:
        status = 204

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def opener(request: object, timeout: int) -> Response:
        bodies.append(json.loads(request.data))
        assert timeout == 15
        return Response()

    result = publish_alert(
        AlertEvent("hira_parse_failed_ratio", "run-1", 10, 200, 0.05, 0.04, 3),
        endpoint="https://alerts.invalid/hira",
        opener=opener,
        sleeper=lambda _: None,
    )

    assert result.status == "published"
    assert bodies[0]["run_id"] == "run-1"
