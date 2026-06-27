from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from scripts.compose_ab_poc.mart_store import MartStore


PRIMITIVE_TOOLS: Final[tuple[str, ...]] = (
    "fetch",
    "filter",
    "group_by",
    "aggregate",
    "join",
    "compute_share",
    "compute_hhi",
    "compute_growth",
    "compute_rank",
    "compute_delta",
    "compute_series",
    "compute_mix",
    "compute_gap",
)

METRICS: Final[tuple[str, ...]] = ("sales", "share", "rank", "hhi", "growth")
DERIVATIONS: Final[tuple[str, ...]] = ("share", "hhi", "growth", "rank", "delta", "trend", "top_n", "mix", "gap", "concentration", "unsupported_dimension")
SORTS: Final[tuple[str, ...]] = ("sales_desc", "share_desc", "rank_asc", "period_asc")
BASE_DIMENSIONS: Final[tuple[str, ...]] = ("channel", "specialty", "molecule", "dosage_form", "nhi_type", "ox_gx", "product", "company")


@dataclass(frozen=True, slots=True)
class CompositionCatalog:
    """Market-specific identifier catalog injected into Flash prompts and validators."""

    market: str
    source: str
    view: str
    primitive_tools: tuple[str, ...]
    dimensions: tuple[str, ...]
    group_by: tuple[str, ...]
    metrics: tuple[str, ...]
    derivations: tuple[str, ...]
    sorts: tuple[str, ...]

    @classmethod
    def from_store(cls, store: MartStore) -> "CompositionCatalog":
        """Build the catalog from mart data actually present for this market."""

        available: list[str] = []
        if any(record.channel_data for record in store.records):
            available.append("channel")
        if any(record.specialty_data for record in store.records):
            available.append("specialty")
        if any(record.molecule and record.molecule != "unknown" for record in store.records):
            available.append("molecule")
        if any(record.class_label and record.class_label != "unknown" for record in store.records):
            available.append("dosage_form")
        if any("nhi_type" in record.dimension_data for record in store.records):
            available.append("nhi_type")
        if any(record.ox_gx and record.ox_gx != "unknown" for record in store.records):
            available.append("ox_gx")
        available.extend(("product", "company"))
        ordered = tuple(item for item in BASE_DIMENSIONS if item in set(available))
        return cls(
            market="ml_006",
            source="ubist",
            view="market_landscape",
            primitive_tools=PRIMITIVE_TOOLS,
            dimensions=ordered,
            group_by=(*ordered, "period"),
            metrics=METRICS,
            derivations=DERIVATIONS,
            sorts=SORTS,
        )

    def prompt_block(self) -> str:
        """Render a compact enum block for the LLM planner."""

        return "\n".join(
            (
                "유효 식별자 catalog(enum):",
                f"- source: {self.source}",
                f"- view: {self.view}",
                f"- market: {self.market}",
                f"- primitive_tools: {', '.join(self.primitive_tools)}",
                f"- dimensions: {', '.join(self.dimensions)}",
                f"- group_by: {', '.join(self.group_by)}",
                f"- metrics: {', '.join(self.metrics)}",
                f"- derive: {', '.join(self.derivations)}",
                f"- sort: {', '.join(self.sorts)}",
                "이 목록 밖의 도구·차원·메트릭·derive·sort 이름은 절대 만들지 마라.",
                "기간축은 group_by의 period만 사용하고 month/date/period_ym 같은 별칭을 쓰지 마라.",
            )
        )
