from __future__ import annotations

from typing import Final, Literal


TOPIC_CONF_RELIABLE_MIN_N: Final = 50
TOPIC_CONF_LOW_MIN_N: Final = 20

TopicConfidence = Literal["reliable", "low", "insufficient"]


def topic_confidence_for_event_count(event_count: int) -> TopicConfidence:
    """Return the topic-share confidence bucket for a brand event count."""
    if event_count >= TOPIC_CONF_RELIABLE_MIN_N:
        return "reliable"
    if event_count >= TOPIC_CONF_LOW_MIN_N:
        return "low"
    return "insufficient"
