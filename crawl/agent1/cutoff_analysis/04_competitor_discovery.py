#!/usr/bin/env python3
"""Infer unknown matched brands' likely ml_market from co-occurrence evidence."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from common import SAMPLES_DIR, ensure_dirs, load_brand_catalog, load_matches, load_market_catalog, write_df


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(SAMPLES_DIR / "unknown_brand_mapping.csv"))
    args = parser.parse_args()

    ensure_dirs()
    df = load_matches()
    catalog_df, _ = load_brand_catalog()
    markets = load_market_catalog().set_index("ml_id")
    known = df[df["brand_group"] != "unknown"]
    unknown = df[df["brand_group"] == "unknown"]
    article_known = {
        article_id: group[["brand", "brand_canonical", "ml_id", "molecule", "class_name"]].dropna(how="all").to_dict("records")
        for article_id, group in known.groupby("article_id")
    }

    rows = []
    for brand, brand_df in unknown.groupby("brand"):
        co_brand_counter: Counter[str] = Counter()
        ml_counter: Counter[str] = Counter()
        molecule_counter: Counter[str] = Counter()
        class_counter: Counter[str] = Counter()
        content_text = " ".join((brand_df["title"].fillna("") + " " + brand_df["summary"].fillna("") + " " + brand_df["content"].fillna("").str[:1200]).tolist())
        for article_id in brand_df["article_id"].unique():
            for row in article_known.get(article_id, []):
                co = row.get("brand_canonical") or row.get("brand")
                if co:
                    co_brand_counter[str(co)] += 1
                if row.get("ml_id"):
                    ml_counter[str(row["ml_id"])] += 1
                if row.get("molecule"):
                    molecule_counter[str(row["molecule"])] += 1
                if row.get("class_name"):
                    class_counter[str(row["class_name"])] += 1
        occurrence = int(len(brand_df))
        top_ml, top_ml_count = (ml_counter.most_common(1)[0] if ml_counter else (None, 0))
        top_share = top_ml_count / max(sum(ml_counter.values()), 1)
        if top_ml_count >= 5 and top_share >= 0.5:
            confidence = "high"
        elif top_ml_count >= 2:
            confidence = "medium"
        else:
            confidence = "low"
        inferred_molecule = molecule_counter.most_common(1)[0][0] if molecule_counter else ""
        inferred_class = class_counter.most_common(1)[0][0] if class_counter else ""
        market_name = ""
        if top_ml and top_ml in markets.index:
            market_name = str(markets.loc[top_ml].get("name", ""))
        rows.append(
            {
                "unknown_brand": brand,
                "occurrence_count": occurrence,
                "top_co_brands": ";".join(f"{name}:{count}" for name, count in co_brand_counter.most_common(8)),
                "candidate_ml_id": top_ml or "",
                "candidate_market_name": market_name,
                "candidate_ml_evidence_count": int(top_ml_count),
                "candidate_ml_share": round(top_share, 3),
                "inferred_class": inferred_class,
                "inferred_molecule": inferred_molecule,
                "confidence": confidence,
                "notes": "co-occurrence based; catalog not modified",
            }
        )
    result = pd.DataFrame(rows).sort_values(["confidence", "occurrence_count"], ascending=[True, False])
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    result["_order"] = result["confidence"].map(confidence_order)
    result = result.sort_values(["_order", "occurrence_count"], ascending=[True, False]).drop(columns=["_order"])
    write_df(result, Path(args.output))
    print(result["confidence"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
