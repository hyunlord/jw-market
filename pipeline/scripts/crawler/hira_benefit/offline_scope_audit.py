# /// script
# requires-python = ">=3.12"
# ///
# How to run:
# PYTHONPATH=. uv run python -m pipeline.scripts.crawler.hira_benefit.offline_scope_audit \
#   --audit-root /path/to/hira_benefit_brand_scope_audit_20260726 \
#   --output /tmp/hira_scope_measurement.json

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TypedDict

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name

from .scope import (
    BrandScopeEntry,
    MoleculeScopeEntry,
    derive_dosage_form_suffixes,
    derive_non_specific_molecules,
    match_brand_scope,
    normalize_scope_text,
)


class LabeledPair(TypedDict):
    brand_name: str
    notice_id: str
    verdict: str


LABEL_FILES = {
    "A": "false_positive_A.txt",
    "B": "false_positive_B.txt",
    "C": "false_positive_C.txt",
    "short": "short_name_risk.txt",
}


def _read_population(root: Path) -> dict[str, list[dict[str, object]]]:
    with gzip.open(root / "raw" / "population.json.gz", "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _labeled_pairs(path: Path) -> tuple[LabeledPair, ...]:
    rows: list[LabeledPair] = []
    for block in re.split(r"\n(?=\[\d+\])", path.read_text(encoding="utf-8")):
        identity = re.search(r"brand_name=(.*)\nsource_notice_id=(.*)\n", block)
        verdict = re.search(r"판정=(정탐|오탐|판단 불가)", block)
        if identity is not None and verdict is not None:
            rows.append(
                {
                    "brand_name": identity.group(1).strip(),
                    "notice_id": identity.group(2).strip(),
                    "verdict": verdict.group(1),
                }
            )
    return tuple(rows)


def _scope(
    population: dict[str, list[dict[str, object]]],
) -> tuple[tuple[BrandScopeEntry, ...], tuple[MoleculeScopeEntry, ...]]:
    aliases = {
        str(row["alias_name"]): str(row["brand_key"])
        for row in population["aliases"]
    }
    atc4_by_key: dict[str, set[str]] = defaultdict(set)
    for row in population["brand_molecules"]:
        atc4_code = str(row["atc4_code"])
        if atc4_code:
            atc4_by_key[str(row["brand_key"])].add(atc4_code)

    brands = [
        BrandScopeEntry(
            brand_key=aliases.get(
                str(row["brand_name"]),
                normalize_brand_name(str(row["brand_name"])),
            ),
            brand_name=str(row["brand_name"]),
            atc4_codes=tuple(
                sorted(
                    atc4_by_key[
                        aliases.get(
                            str(row["brand_name"]),
                            normalize_brand_name(str(row["brand_name"])),
                        )
                    ]
                )
            ),
        )
        for row in population["candidate_b"]
    ]
    existing = {(row.brand_key, row.brand_name) for row in brands}
    canonical_keys = {row.brand_key for row in brands}
    for alias_name, brand_key in aliases.items():
        if brand_key in canonical_keys and (brand_key, alias_name) not in existing:
            brands.append(
                BrandScopeEntry(
                    brand_key=brand_key,
                    brand_name=alias_name,
                    atc4_codes=tuple(sorted(atc4_by_key[brand_key])),
                )
            )
    molecules = tuple(
        MoleculeScopeEntry(
            molecule_norm=str(row["molecule_norm"]),
            brand_key=str(row["brand_key"]),
            brand_name=str(row["brand_name"]),
            atc4_code=str(row["atc4_code"]),
        )
        for row in population["brand_molecules"]
    )
    return tuple(brands), molecules


def measure(root: Path) -> dict[str, object]:
    started = time.monotonic()
    population = _read_population(root)
    brands, molecules = _scope(population)
    notices = {
        str(row["source_notice_id"]): row for row in population["notices"]
    }
    blocked = derive_non_specific_molecules(
        molecules,
        tuple(str(row["raw_text"]) for row in notices.values()),
    )
    dosage_form_suffixes = derive_dosage_form_suffixes(
        brands,
        tuple(str(row["raw_text"]) for row in notices.values()),
    )
    matches_by_notice = {
        notice_id: match_brand_scope(
            str(notice["raw_text"]),
            brands,
            molecules,
            blocked_molecules=blocked,
            dosage_form_suffixes=dosage_form_suffixes,
        )
        for notice_id, notice in notices.items()
    }

    label_metrics: dict[str, object] = {}
    for label, filename in LABEL_FILES.items():
        counts: Counter[str] = Counter()
        rows: list[dict[str, object]] = []
        for pair in _labeled_pairs(root / "evidence" / filename):
            matched_names = {
                match.brand_name for match in matches_by_notice[pair["notice_id"]]
            }
            matched = pair["brand_name"] in matched_names
            true_match = pair["verdict"] == "정탐"
            outcome = (
                "TP"
                if true_match and matched
                else "FN"
                if true_match
                else "FP"
                if matched
                else "TN"
            )
            counts[outcome] += 1
            rows.append({**pair, "matched": matched, "outcome": outcome})
        label_metrics[label] = {"counts": dict(counts), "rows": rows}

    pairs = [
        {
            "source_notice_id": notice_id,
            "brand_key": match.brand_key,
            "brand_name": match.brand_name,
            "match_method": match.match_method,
            "confidence": match.confidence,
            "matched_text": match.matched_text,
            "molecule_norm": match.molecule_norm,
            "atc4_code": match.atc4_code,
            "evidence_start": match.evidence_start,
            "evidence_end": match.evidence_end,
            "evidence_coordinate": match.evidence_coordinate,
        }
        for notice_id, matches in matches_by_notice.items()
        for match in matches
    ]
    old_pairs = {
        (str(row["source_notice_id"]), str(row["brand_name"]))
        for row in population["notice_brands"]
    }
    new_pairs = {
        (str(row["source_notice_id"]), str(row["brand_name"])) for row in pairs
    }
    eylea_notice_ids = {
        str(row["source_notice_id"])
        for row in pairs
        if normalize_brand_name(str(row["brand_name"]))
        == normalize_brand_name("아일리아")
    }
    def sample_with_context(
        candidates: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        sample = random.Random(20260726).sample(
            candidates, min(30, len(candidates))
        )
        for row in sample:
            text = normalize_scope_text(
                str(notices[str(row["source_notice_id"])]["raw_text"])
            )
            start = int(row["evidence_start"])
            end = int(row["evidence_end"])
            row["snippet"] = text[
                max(0, start - 100) : min(len(text), end + 200)
            ]
        return sample

    sample = sample_with_context(pairs)
    molecule_sample = sample_with_context(
        [
            row
            for row in pairs
            if row["match_method"] == "molecule_via_atc4"
        ]
    )

    candidate_b = {
        normalize_brand_name(str(row["brand_name"]))
        for row in population["candidate_b"]
    }
    candidate_c = {
        str(row["brand_key"]) for row in population["candidate_c"]
    }
    collision_products = (
        "리바로페노",
        "리바로하이",
        "라베칸듀오",
        "리바로브이",
        "리바로젯",
        "위너프A+",
        "위너프에이플러스",
    )
    return {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "universe": {
            "brands": len(brands),
            "brand_keys": len({row.brand_key for row in brands}),
            "molecules": len(molecules),
            "blocked_molecule_count": len(blocked),
            "blocked_molecules": sorted(blocked),
            "dosage_form_suffix_count": len(dosage_form_suffixes),
            "dosage_form_suffixes": sorted(dosage_form_suffixes),
        },
        "membership": {
            name: {
                "B": normalize_brand_name(name) in candidate_b,
                "C": normalize_brand_name(name) in candidate_c,
            }
            for name in collision_products
        },
        "label_metrics": label_metrics,
        "hard_gate": {
            "eylea_notice_count": len(eylea_notice_ids),
            "eylea_notice_ids": sorted(eylea_notice_ids),
        },
        "full": {
            "notice_count": len(notices),
            "matched_notice_count": len(
                {str(row["source_notice_id"]) for row in pairs}
            ),
            "pair_count": len(pairs),
            "brand_count": len({str(row["brand_key"]) for row in pairs}),
            "method_counts": dict(
                Counter(str(row["match_method"]) for row in pairs)
            ),
            "old_pair_count": len(old_pairs),
            "removed": [
                {"source_notice_id": notice_id, "brand_name": brand_name}
                for notice_id, brand_name in sorted(old_pairs - new_pairs)
            ],
            "added": [
                {"source_notice_id": notice_id, "brand_name": brand_name}
                for notice_id, brand_name in sorted(new_pairs - old_pairs)
            ],
        },
        "sample_30": sample,
        "molecule_sample_30": molecule_sample,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.audit_root)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    full = result["full"]
    print(
        json.dumps(
            {
                "notice_count": full["notice_count"],
                "matched_notice_count": full["matched_notice_count"],
                "pair_count": full["pair_count"],
                "brand_count": full["brand_count"],
                "method_counts": full["method_counts"],
                "removed_count": len(full["removed"]),
                "added_count": len(full["added"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
