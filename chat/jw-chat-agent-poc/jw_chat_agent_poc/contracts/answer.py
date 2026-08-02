from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from .base import ContractModel


class SectionKind(StrEnum):
    TEXT = "text"
    TABLE = "table"
    CHART = "chart"


class SupportedClaim(ContractModel):
    claim_type: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: Decimal | str | None = None
    evidence_ids: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()


class TableCell(ContractModel):
    evidence_id: str = Field(min_length=1)
    display_value: str | None = None


class TableRow(ContractModel):
    cells: tuple[TableCell, ...] = Field(min_length=1)


class ChartIntent(ContractModel):
    chart_id: str = Field(min_length=1)
    chart_type: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    title: str | None = None


class AnswerSection(ContractModel):
    section_id: str = Field(min_length=1)
    kind: SectionKind
    evidence_ids: tuple[str, ...] = ()
    text: str | None = None
    table_rows: tuple[TableRow, ...] = ()
    chart_intent: ChartIntent | None = None

    @model_validator(mode="after")
    def require_matching_payload(self) -> Self:
        match self.kind:
            case SectionKind.TEXT:
                if self.text is None:
                    raise ValueError("text section requires text")
            case SectionKind.TABLE:
                if not self.table_rows:
                    raise ValueError("table section requires evidence-referencing rows")
            case SectionKind.CHART:
                if self.chart_intent is None:
                    raise ValueError("chart section requires ChartIntent")
        return self


class ContractFacetFailure(ContractModel):
    facet: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class AnswerModel(ContractModel):
    title: str = Field(min_length=1)
    claims: tuple[SupportedClaim, ...] = ()
    sections: tuple[AnswerSection, ...] = ()
    notices: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    requested_facets: tuple[str, ...] = ()
    unresolvable_facets: tuple[ContractFacetFailure, ...] = ()

    @model_validator(mode="after")
    def bind_unresolvable_facets_to_request(self) -> Self:
        requested = frozenset(self.requested_facets)
        if any(item.facet not in requested for item in self.unresolvable_facets):
            raise ValueError("unresolvable facet must be present in requested_facets")
        return self
