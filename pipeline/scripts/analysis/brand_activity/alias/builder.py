from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal

from pipeline.scripts.analysis.brand_activity.alias.normalize import (
    configured_variant_rule_count,
    configured_variants_for,
    normalize_iqvia_en,
)


SourceName = Literal["csd", "keyword", "meeting"]
MappingStatus = Literal["confirmed", "inferred", "pending"]
CSD_UNCOVERED_ATC4 = frozenset({"L04B0", "L04D0", "M01C0", "L03A1", "B01C5", "A06B1"})

CSD_MARKET_ATC4_INFERENCE: dict[str, tuple[str, ...]] = {
    "FERINJECT Market": ("B03A1",),
    "GANAKHAN Market": ("A03F0",),
    "GUARDLET Market": ("A10N3",),
    "LIVALO Market": ("C10A1",),
    "LIVALO V Market": ("C11A1",),
    "LIVALOZET Market": ("C10C0",),
    "PPI Market": ("A02B2",),
    "TURUPAS Market": ("G04C2",),
    "WINUF Market": ("K01D2",),
}


@dataclass(frozen=True, slots=True)
class SourceObservation:
    source: SourceName
    product_name: str
    atc4: str
    csd_market: str
    representing_company: str
    manufacturer: str | None


@dataclass(frozen=True, slots=True)
class KorEvidence:
    kr_name: str
    evidence_type: str
    evidence_source: str


@dataclass(frozen=True, slots=True)
class AliasRecord:
    iqvia_en: str
    variants: tuple[str, ...]
    sources: dict[str, bool]
    atc4: tuple[str, ...]
    csd_market: tuple[str, ...]
    kr_canonical: str | None
    is_jw: bool
    manufacturer: tuple[str, ...]
    representing_company: tuple[str, ...]
    molecule: tuple[str, ...]
    csd_uncovered: bool
    mapping_status: MappingStatus
    note: str
    evidence: dict[str, object]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "iqvia_en": self.iqvia_en,
            "variants": list(self.variants),
            "sources": self.sources,
            "atc4": list(self.atc4),
            "csd_market": list(self.csd_market),
            "kr_canonical": self.kr_canonical,
            "is_jw": self.is_jw,
            "manufacturer": list(self.manufacturer),
            "representing_company": list(self.representing_company),
            "molecule": list(self.molecule),
            "csd_uncovered": self.csd_uncovered,
            "mapping_status": self.mapping_status,
            "note": self.note,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class AliasStats:
    anchor_count: int
    configured_variant_rule_count: int
    observed_multi_variant_rule_count: int
    csd_uncovered_count: int
    status_distribution: dict[str, int]
    jw_mapped_count: int
    atc4_attached_count: int


@dataclass(frozen=True, slots=True)
class AliasBuildResult:
    records: tuple[AliasRecord, ...]
    stats: AliasStats

    @property
    def by_anchor(self) -> dict[str, AliasRecord]:
        return {record.iqvia_en: record for record in self.records}


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value and value.strip()}))


def _source_flags(observations: list[SourceObservation]) -> dict[str, bool]:
    present = {observation.source for observation in observations}
    return {
        "csd": "csd" in present,
        "keyword": "keyword" in present,
        "meeting": "meeting" in present,
    }


def _atc4_values(observations: list[SourceObservation]) -> tuple[str, ...]:
    direct = [observation.atc4.strip().upper() for observation in observations if observation.atc4.strip()]
    inferred = [
        atc4
        for observation in observations
        if not observation.atc4.strip() and observation.csd_market in CSD_MARKET_ATC4_INFERENCE
        for atc4 in CSD_MARKET_ATC4_INFERENCE[observation.csd_market]
    ]
    return _unique(direct + inferred)


def _kr_status(
    anchor: str,
    evidence: KorEvidence | None,
    jw_canonicals: set[str],
    explicit_jw_aliases: dict[str, str],
) -> tuple[str | None, bool, MappingStatus, list[str]]:
    notes: list[str] = []
    if evidence is not None and evidence.kr_name in jw_canonicals:
        return evidence.kr_name, True, "confirmed", [
            f"KOR confirmed by {evidence.evidence_type}: {evidence.evidence_source}"
        ]
    if anchor in explicit_jw_aliases:
        status: MappingStatus = "inferred"
        if evidence is not None:
            notes.append(f"official KOR observed: {evidence.kr_name} ({evidence.evidence_source})")
        notes.append("JW canonical inferred from catalog/fallback alias; PL review recommended")
        return explicit_jw_aliases[anchor], True, status, notes
    if evidence is not None:
        return None, False, "confirmed", [
            f"non-JW official KOR observed: {evidence.kr_name} ({evidence.evidence_source})"
        ]
    return None, False, "pending", ["no Korean canonical evidence found"]


def build_alias_records(
    observations: list[SourceObservation],
    kor_evidence: dict[str, KorEvidence],
    jw_canonicals: set[str],
    explicit_jw_aliases: dict[str, str] | None = None,
    molecule_by_anchor: dict[str, tuple[str, ...]] | None = None,
    extra_atc4_by_anchor: dict[str, tuple[str, ...]] | None = None,
    manufacturer_by_anchor: dict[str, tuple[str, ...]] | None = None,
) -> AliasBuildResult:
    aliases = explicit_jw_aliases or {}
    molecules = molecule_by_anchor or {}
    extra_atc4 = extra_atc4_by_anchor or {}
    extra_manufacturers = manufacturer_by_anchor or {}
    grouped: defaultdict[str, list[SourceObservation]] = defaultdict(list)
    observed_by_anchor: defaultdict[str, set[str]] = defaultdict(set)
    for observation in observations:
        anchor = normalize_iqvia_en(observation.product_name)
        grouped[anchor].append(observation)
        observed_by_anchor[anchor].add(observation.product_name.strip().upper())

    records: list[AliasRecord] = []
    for anchor in sorted(grouped):
        rows = grouped[anchor]
        sources = _source_flags(rows)
        atc4 = _unique([*_atc4_values(rows), *extra_atc4.get(anchor, ())])
        markets = _unique([row.csd_market for row in rows])
        configured_variants = configured_variants_for(anchor)
        if anchor in observed_by_anchor and anchor in {normalize_iqvia_en(name) for name in configured_variants}:
            variants = configured_variants
        else:
            variants = _unique([row.product_name.upper() for row in rows])
        kr_canonical, is_jw, status, notes = _kr_status(
            anchor,
            kor_evidence.get(anchor),
            jw_canonicals,
            aliases,
        )
        csd_uncovered = not sources["csd"] and bool(set(atc4) & CSD_UNCOVERED_ATC4)
        if csd_uncovered:
            notes.append("CSD uncovered ATC4; do not force-map to CSD market")
        if not sources["csd"] and not csd_uncovered:
            notes.append("Keyword/Meeting-only product outside current uncovered-class list")
        if sources["csd"] and any(row.atc4 == "" for row in rows):
            notes.append("ATC4 for CSD rows inferred from CSD market where mapping exists")
        if extra_atc4.get(anchor):
            notes.append("ATC4 supplemented from local NSA PRODUCT NAME match")
        evidence_columns = []
        if sources["csd"]:
            evidence_columns.append("csd_channel_dynamics_stage.master_product")
        if sources["keyword"]:
            evidence_columns.append("km_keyword_event_stage.product_name")
        if sources["meeting"]:
            evidence_columns.append("km_meeting_event_stage.product_name")
        kor_source = kor_evidence.get(anchor)
        records.append(
            AliasRecord(
                iqvia_en=anchor,
                variants=variants,
                sources=sources,
                atc4=atc4,
                csd_market=markets,
                kr_canonical=kr_canonical,
                is_jw=is_jw,
                manufacturer=_unique([row.manufacturer or "" for row in rows] + list(extra_manufacturers.get(anchor, ()))),
                representing_company=_unique([row.representing_company for row in rows]),
                molecule=molecules.get(anchor, ()),
                csd_uncovered=csd_uncovered,
                mapping_status=status,
                note="; ".join(dict.fromkeys(notes)),
                evidence={
                    "source_columns": evidence_columns,
                    "atc4_columns": [
                        "km_keyword_event_stage.therapeutic_class",
                        "km_meeting_event_stage.therapeutic_class",
                        "csd_channel_dynamics_stage.market",
                        "data/IQVIA/NSA/*.csv:ATC 4 CODE",
                    ],
                    "company_columns": [
                        "csd_channel_dynamics_stage.representing_company",
                        "km_keyword_event_stage.representing_company",
                        "data/IQVIA/NSA/*.csv:MFR NAME/MFR NAME KOR",
                    ],
                    "kor_evidence": None
                    if kor_source is None
                    else {
                        "type": kor_source.evidence_type,
                        "source": kor_source.evidence_source,
                    },
                },
            )
        )

    observed_multi_variant_rules = sum(
        1
        for anchor, variants in (
            (anchor, tuple(value.upper() for value in configured_variants_for(anchor)))
            for anchor in ("APITO", "LOW OSMO PERI")
        )
        if len(observed_by_anchor.get(anchor, set()) & set(variants)) > 1
    )
    status_counts = Counter(record.mapping_status for record in records)
    mapped_jw_canonicals = {record.kr_canonical for record in records if record.kr_canonical in jw_canonicals}
    stats = AliasStats(
        anchor_count=len(records),
        configured_variant_rule_count=configured_variant_rule_count(),
        observed_multi_variant_rule_count=observed_multi_variant_rules,
        csd_uncovered_count=sum(1 for record in records if record.csd_uncovered),
        status_distribution=dict(sorted(status_counts.items())),
        jw_mapped_count=len(mapped_jw_canonicals),
        atc4_attached_count=sum(1 for record in records if record.atc4),
    )
    return AliasBuildResult(records=tuple(records), stats=stats)
