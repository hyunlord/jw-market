from __future__ import annotations

from .data_source import sample_brand_rows, sample_scope_rows
from .models import KeywordRow, ScopeSpec


SCOPE_SPECS = (
    ScopeSpec(
        scope_id="group:livalo_family",
        label="리바로 시장군(C10A1+C10C0)",
        atc4_values=("C10A1", "C10C0"),
        axis_brands=("LIVALO", "LIVALOZET", "LIPITOR", "ATOZET"),
        share_brands=(("C10A1", "LIVALO"), ("C10C0", "LIVALOZET"), ("C10C0", "ATOZET")),
        scope_type="multi_atc4_group",
    ),
    ScopeSpec(
        scope_id="atc4:G04C2",
        label="G04C2 트루패스 시장",
        atc4_values=("G04C2",),
        axis_brands=("THRUPAS", "HANMITAMS", "FLIVAS"),
        share_brands=(("G04C2", "THRUPAS"), ("G04C2", "HANMITAMS"), ("G04C2", "FLIVAS")),
        scope_type="single_atc4",
    ),
    ScopeSpec(
        scope_id="group:winuf_family",
        label="위너프 시장군(K01D2)",
        atc4_values=("K01D2",),
        axis_brands=("WINUF", "WINUF PERI", "WINUF A PLUS"),
        share_brands=(("K01D2", "WINUF"), ("K01D2", "WINUF PERI"), ("K01D2", "WINUF A PLUS")),
        scope_type="single_atc4_group",
    ),
)


def all_atc4_values() -> tuple[str, ...]:
    """Return every ATC4 needed by the bounded comparison scopes."""
    values: list[str] = []
    for scope in SCOPE_SPECS:
        values.extend(scope.atc4_values)
    return tuple(sorted(set(values)))


def rows_for_scope(rows: list[KeywordRow], scope: ScopeSpec) -> list[KeywordRow]:
    """Filter keyword rows to a comparison scope."""
    return [row for row in rows if row.atc4 in scope.atc4_values]


def build_axis_samples(rows: list[KeywordRow], *, per_brand: int) -> dict[str, list[KeywordRow]]:
    """Create deterministic market/group axis samples for all scopes."""
    samples: dict[str, list[KeywordRow]] = {}
    for scope in SCOPE_SPECS:
        scope_rows = rows_for_scope(rows, scope)
        samples[scope.scope_id] = sample_scope_rows(scope_rows, scope.axis_brands, per_brand=per_brand, seed=f"axis:{scope.scope_id}")
    return samples


def build_brand_samples(rows: list[KeywordRow], *, limit: int) -> dict[str, list[KeywordRow]]:
    """Create deterministic brand-share samples for all scoped brands."""
    samples: dict[str, list[KeywordRow]] = {}
    for scope in SCOPE_SPECS:
        for atc4, brand in scope.share_brands:
            samples[f"{scope.scope_id}:{atc4}:{brand}"] = sample_brand_rows(rows, atc4, brand, limit=limit)
    return samples


def scope_by_id() -> dict[str, ScopeSpec]:
    """Index comparison scopes by stable scope id."""
    return {scope.scope_id: scope for scope in SCOPE_SPECS}
