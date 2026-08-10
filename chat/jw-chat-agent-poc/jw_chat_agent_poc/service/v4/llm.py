from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import os

from jw_chat_agent_poc.genos_config import (
    resolve_final_genos_base_url,
    resolve_final_genos_token,
    resolve_planner_genos_base_url,
    resolve_planner_genos_token,
)
from jw_chat_agent_poc.service.genos_client import GenosClient


PLANNER_MODEL = "gemini-3.1-pro-preview"
SYNTHESIZER_MODEL = "gemini-3-flash-preview"


class GenOSV4Client:
    """Small GenOS serving facade that bypasses the legacy finalizer."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        model: str,
        timeout_s: int,
        total_budget_s: int,
    ) -> None:
        self._client = GenosClient(
            base_url=base_url,
            token=token,
            timeout_s=timeout_s,
            total_budget_s=total_budget_s,
            model=model,
        )

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        budget_s: float | None = None,
    ) -> str:
        client = self._client
        if budget_s is not None:
            bounded = max(1, int(budget_s))
            client = replace(
                client,
                timeout_s=min(client.timeout_s, bounded),
                total_budget_s=min(client.total_budget_s, bounded),
            )
        return client._chat_text(list(messages))  # noqa: SLF001 - direct GenOS transport reuse

    @property
    def serving_id(self) -> str:
        marker = "/serving/"
        base_url = self._client.base_url.rstrip("/")
        return base_url.rsplit(marker, 1)[-1].split("/", 1)[0] if marker in base_url else "unknown"


def planner_client() -> GenOSV4Client:
    return GenOSV4Client(
        base_url=resolve_planner_genos_base_url(),
        token=resolve_planner_genos_token(),
        model=os.environ.get("V4_PLANNER_MODEL", PLANNER_MODEL),
        timeout_s=int(os.environ.get("V4_PLANNER_TIMEOUT_S", "18")),
        total_budget_s=int(os.environ.get("V4_PLANNER_BUDGET_S", "24")),
    )


def synthesizer_client() -> GenOSV4Client:
    return GenOSV4Client(
        base_url=resolve_final_genos_base_url(),
        token=resolve_final_genos_token(),
        model=os.environ.get("V4_SYNTHESIZER_MODEL", SYNTHESIZER_MODEL),
        timeout_s=int(os.environ.get("V4_SYNTHESIZER_TIMEOUT_S", "15")),
        total_budget_s=int(os.environ.get("V4_SYNTHESIZER_BUDGET_S", "20")),
    )
