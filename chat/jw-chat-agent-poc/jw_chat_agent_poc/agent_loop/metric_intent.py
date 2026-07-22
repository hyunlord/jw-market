from __future__ import annotations

import re


def explicit_base_metrics_from_question(question: str) -> tuple[str, ...]:
    metrics: list[str] = []
    if re.search(r"처방\s*량|prescription\s+volume", question, re.IGNORECASE):
        metrics.append("prescription_volume")
    if re.search(r"매출|판매|(?<![A-Za-z])sales(?![A-Za-z])", question, re.IGNORECASE):
        metrics.append("sales")
    return tuple(metrics)
