#!/usr/bin/env python3
"""Evaluate the processed GCP news corpus for Agent 2 readiness."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


EXPECTED_TAGS = {"신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드"}
BOILERPLATE_PATTERN = re.compile(r"직접\s*경쟁약.*동일\s*점수\s*적용|동일\s*점수\s*적용")

TAG_HINTS = {
    "신약/R&D": ["임상", "허가", "신약", "개발", "R&D", "적응증", "연구", "파이프라인"],
    "정책/규제": ["식약처", "급여", "약가", "보험", "허가", "특허", "규제", "재심사"],
    "공급/생산": ["공급", "생산", "품절", "공장", "수급", "위탁", "제조"],
    "자본/경영": ["매출", "영업", "계약", "인수", "합병", "실적", "코프로모션", "투자"],
    "외부/트렌드": ["시장", "트렌드", "전망", "환자", "글로벌", "성장", "경쟁"],
}


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def media_name(path: Path) -> str:
    name = path.parent.name
    return name.replace("news_5years_", "").replace("_processed", "")


def processed_dirs(corpus: Path) -> list[Path]:
    return sorted(p for p in corpus.iterdir() if p.is_dir() and p.name.endswith("_processed"))


def choose_sample(corpus: Path, sample_size: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    dirs = processed_dirs(corpus)
    if not dirs:
        raise RuntimeError(f"No *_processed dirs found under {corpus}")
    base = sample_size // len(dirs)
    remainder = sample_size % len(dirs)
    samples: list[dict[str, Any]] = []
    for idx, directory in enumerate(dirs):
        files = sorted(directory.glob("*.json"))
        n = min(base + (1 if idx < remainder else 0), len(files))
        for path in rng.sample(files, n):
            data = read_json(path)
            data["_path"] = str(path)
            data["_media"] = media_name(path)
            samples.append(data)
    rng.shuffle(samples)
    return samples


def load_catalog_names(strategic_brand_path: Path, cd_brand_path: Path | None = None) -> tuple[set[str], pd.DataFrame]:
    frames = [pd.read_parquet(strategic_brand_path)]
    if cd_brand_path and cd_brand_path.exists():
        frames.append(pd.read_parquet(cd_brand_path))
    catalog = pd.concat(frames, ignore_index=True)
    names: set[str] = set()
    for column in ["name", "merge_name", "canonical_name", "general_brand_key"]:
        if column in catalog.columns:
            names.update(str(v).strip() for v in catalog[column].dropna().unique() if str(v).strip())
    return names, catalog


def score_bucket(score: Any) -> str:
    try:
        value = int(score)
    except Exception:
        value = 0
    if value < 10:
        return "0-9"
    if value < 20:
        return "10-19"
    if value < 30:
        return "20-29"
    if value < 40:
        return "30-39"
    if value < 50:
        return "40-49"
    if value < 60:
        return "50-59"
    if value < 70:
        return "60-69"
    if value < 80:
        return "70-79"
    if value < 90:
        return "80-89"
    if value < 95:
        return "90-94"
    return "95-100"


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def article_url(item: dict[str, Any]) -> str | None:
    sources = item.get("sources") or []
    if sources and isinstance(sources[0], dict):
        return sources[0].get("url")
    return None


def title_content_consistent(item: dict[str, Any]) -> bool:
    title = str(item.get("title") or "")
    haystack = " ".join([str(item.get("summary") or ""), str(item.get("content") or "")])
    tokens = [token for token in re.split(r"[\s,.'\"·/()\\[\\]-]+", title) if len(token) >= 3]
    if not tokens:
        return bool(haystack)
    return any(token in haystack for token in tokens[:8])


def tag_consistent(item: dict[str, Any]) -> bool | None:
    tag = item.get("tag")
    if tag not in TAG_HINTS:
        return None
    haystack = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("summary") or ""),
            str(item.get("content") or "")[:1000],
        ]
    )
    return any(hint in haystack for hint in TAG_HINTS[tag])


@dataclass
class EvalResult:
    samples: list[dict[str, Any]]
    metrics: dict[str, Any]


def evaluate(samples: list[dict[str, Any]], catalog_names: set[str]) -> EvalResult:
    tag_dist = collections.Counter(item.get("tag") for item in samples)
    unexpected_tags = sorted(str(tag) for tag in tag_dist if tag not in EXPECTED_TAGS and tag is not None)
    media_dist = collections.Counter(item.get("_media") for item in samples)

    matches = [match for item in samples for match in item.get("matches", []) if isinstance(match, dict)]
    total_matches = len(matches)
    in_catalog = [m for m in matches if str(m.get("drug") or "").strip() in catalog_names]
    outside = [str(m.get("drug") or "").strip() for m in matches if str(m.get("drug") or "").strip() not in catalog_names]
    outside_counter = collections.Counter(outside)

    score_dist = collections.Counter(score_bucket(m.get("score", 0)) for m in matches)
    score_zero_rows = [m for m in matches if int(m.get("score") or 0) < 10]

    reasons = [str(m.get("reason") or "") for m in matches]
    boilerplate = [reason for reason in reasons if BOILERPLATE_PATTERN.search(reason)]
    reason_lengths = [len(reason) for reason in reasons]

    content_lengths = [len(str(item.get("content") or "")) for item in samples]
    dates = [parse_date(item.get("date")) for item in samples]
    valid_dates = [d for d in dates if d is not None]
    year_dist = collections.Counter(str(d.year) for d in valid_dates)
    sorted_dates = sorted(valid_dates)
    median_date = sorted_dates[len(sorted_dates) // 2].isoformat() if sorted_dates else None
    recent_cutoff = date(2025, 5, 22)

    title_consistency_rows = [title_content_consistent(item) for item in samples]
    tag_check_sample = samples[:20]
    tag_check_values = [tag_consistent(item) for item in tag_check_sample]
    tag_check_known = [value for value in tag_check_values if value is not None]

    score_by_brand: dict[str, list[int]] = collections.defaultdict(list)
    for m in matches:
        drug = str(m.get("drug") or "").strip()
        try:
            score_by_brand[drug].append(int(m.get("score") or 0))
        except Exception:
            score_by_brand[drug].append(0)
    score_outliers = []
    for drug, scores in score_by_brand.items():
        if len(scores) < 3:
            continue
        spread = max(scores) - min(scores)
        if spread >= 50:
            score_outliers.append({"drug": drug, "count": len(scores), "min": min(scores), "max": max(scores), "spread": spread})
    score_outliers.sort(key=lambda row: row["spread"], reverse=True)

    metrics = {
        "sample_count": len(samples),
        "media_distribution": dict(media_dist),
        "tag_distribution": {str(k): v for k, v in tag_dist.items()},
        "unexpected_tags": unexpected_tags,
        "tag_consistency_heuristic_20": {
            "checked": len(tag_check_sample),
            "evaluable": len(tag_check_known),
            "consistent": sum(1 for value in tag_check_known if value),
            "rate_pct": round(sum(1 for value in tag_check_known if value) / max(len(tag_check_known), 1) * 100, 2),
        },
        "brand_matching": {
            "total_matches": total_matches,
            "in_catalog": len(in_catalog),
            "in_catalog_pct": round(len(in_catalog) / max(total_matches, 1) * 100, 2),
            "outside_catalog_unique": len(outside_counter),
            "outside_catalog_top20": outside_counter.most_common(20),
        },
        "score_distribution": {bucket: score_dist.get(bucket, 0) for bucket in [
            "0-9", "10-19", "20-29", "30-39", "40-49", "50-59",
            "60-69", "70-79", "80-89", "90-94", "95-100",
        ]},
        "score_zero_rows": len(score_zero_rows),
        "score_zero_pct": round(len(score_zero_rows) / max(total_matches, 1) * 100, 2),
        "score_outliers_top20": score_outliers[:20],
        "reason_quality": {
            "avg_len": round(sum(reason_lengths) / max(len(reason_lengths), 1), 2),
            "empty_count": sum(1 for reason in reasons if not reason.strip()),
            "boilerplate_count": len(boilerplate),
            "boilerplate_pct": round(len(boilerplate) / max(total_matches, 1) * 100, 2),
            "good_reason_samples": [reason for reason in reasons if len(reason) >= 80 and not BOILERPLATE_PATTERN.search(reason)][:5],
            "boilerplate_samples": boilerplate[:5],
        },
        "content": {
            "avg_len": round(sum(content_lengths) / max(len(content_lengths), 1), 2),
            "min_len": min(content_lengths) if content_lengths else 0,
            "max_len": max(content_lengths) if content_lengths else 0,
            "empty_lt_100": sum(1 for length in content_lengths if length < 100),
            "loaded_pct": round(sum(1 for length in content_lengths if length >= 100) / max(len(content_lengths), 1) * 100, 2),
            "title_content_consistency_pct": round(sum(1 for ok in title_consistency_rows if ok) / max(len(title_consistency_rows), 1) * 100, 2),
        },
        "time": {
            "year_distribution": dict(sorted(year_dist.items())),
            "median_date": median_date,
            "recent_1y_count": sum(1 for d in valid_dates if d >= recent_cutoff),
            "recent_1y_pct": round(sum(1 for d in valid_dates if d >= recent_cutoff) / max(len(valid_dates), 1) * 100, 2),
            "valid_date_count": len(valid_dates),
        },
        "sample_articles_for_manual_review": [
            {
                "title": item.get("title"),
                "date": item.get("date"),
                "tag": item.get("tag"),
                "summary": item.get("summary"),
                "url": article_url(item),
                "matches": item.get("matches", [])[:3],
            }
            for item in tag_check_sample
        ],
    }

    checks = {
        "tag_consistency_ge_90": metrics["tag_consistency_heuristic_20"]["rate_pct"] >= 90,
        "brand_catalog_ge_80": metrics["brand_matching"]["in_catalog_pct"] >= 80,
        "score_zero_lt_30": metrics["score_zero_pct"] < 30,
        "boilerplate_lt_30": metrics["reason_quality"]["boilerplate_pct"] < 30,
        "content_loaded_ge_90": metrics["content"]["loaded_pct"] >= 90,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if len(failed) == 0:
        branch = "A"
    elif len(failed) <= 2:
        branch = "B"
    else:
        branch = "C"
    metrics["branch_decision"] = {"branch": branch, "checks": checks, "failed": failed}
    return EvalResult(samples=samples, metrics=metrics)


def write_markdown(metrics: dict[str, Any], path: Path) -> None:
    b = metrics["branch_decision"]
    tag = metrics["tag_distribution"]
    brand = metrics["brand_matching"]
    reason = metrics["reason_quality"]
    content = metrics["content"]
    time = metrics["time"]
    lines = [
        "# B-1.1 Corpus Quality Evaluation",
        "",
        "## Sample",
        f"- {metrics['sample_count']} random samples, media-balanced",
        "- seed: 42",
        f"- media distribution: `{json.dumps(metrics['media_distribution'], ensure_ascii=False)}`",
        "",
        "## V-1. Category distribution",
    ]
    for name in ["신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드"]:
        lines.append(f"- {name}: {tag.get(name, 0)}")
    lines.extend(
        [
            f"- unexpected tags: {len(metrics['unexpected_tags'])} ({', '.join(metrics['unexpected_tags']) or 'none'})",
            f"- tag consistency heuristic, first 20: {metrics['tag_consistency_heuristic_20']['consistent']} / {metrics['tag_consistency_heuristic_20']['evaluable']} ({metrics['tag_consistency_heuristic_20']['rate_pct']}%)",
            "",
            "## V-2. Brand matching accuracy",
            f"- total matches: {brand['total_matches']:,}",
            f"- in catalog: {brand['in_catalog']:,} ({brand['in_catalog_pct']}%)",
            f"- outside catalog unique: {brand['outside_catalog_unique']:,}",
            "- outside catalog top 20:",
        ]
    )
    for name, count in brand["outside_catalog_top20"]:
        lines.append(f"  - {name}: {count}")
    lines.extend(["", "## V-3. Score distribution", "| bucket | count |", "|---|---:|"])
    for bucket, count in metrics["score_distribution"].items():
        lines.append(f"| {bucket} | {count:,} |")
    lines.extend(
        [
            f"- score 0-9 rows: {metrics['score_zero_rows']:,} ({metrics['score_zero_pct']}%)",
            f"- score outlier brands (spread >= 50): {len(metrics['score_outliers_top20'])} shown in metrics JSON",
            "",
            "## V-4. Reason quality",
            f"- avg len: {reason['avg_len']} chars",
            f"- empty reason count: {reason['empty_count']}",
            f"- boilerplate count: {reason['boilerplate_count']:,} ({reason['boilerplate_pct']}%)",
            "",
            "### Good reason samples",
        ]
    )
    for sample in reason["good_reason_samples"]:
        lines.append(f"- {sample}")
    lines.append("")
    lines.append("### Boilerplate samples")
    for sample in reason["boilerplate_samples"]:
        lines.append(f"- {sample}")
    lines.extend(
        [
            "",
            "## V-5. Content",
            f"- avg len: {content['avg_len']} chars",
            f"- empty (<100): {content['empty_lt_100']}",
            f"- loaded pct: {content['loaded_pct']}%",
            f"- title-content consistency heuristic: {content['title_content_consistency_pct']}%",
            "",
            "## V-6. Time",
            f"- year distribution: `{json.dumps(time['year_distribution'], ensure_ascii=False)}`",
            f"- median date: {time['median_date']}",
            f"- recent 1y: {time['recent_1y_count']} ({time['recent_1y_pct']}%)",
            "",
            "## 결론",
            f"- Branch: **{b['branch']}**",
            f"- checks: `{json.dumps(b['checks'], ensure_ascii=False)}`",
            f"- failed: {', '.join(b['failed']) or 'none'}",
        ]
    )
    if b["branch"] == "A":
        lines.append("- Corpus 품질은 B-1.2 이후 단계에 그대로 활용 가능한 수준입니다.")
    elif b["branch"] == "B":
        lines.extend(
            [
                "",
                "## 분기 B 보완 logic",
                "1. catalog outside brand는 unknown_brand로 적재하되 top unknown list를 audit에 남깁니다.",
                "2. boilerplate reason은 적재하되 Agent 2 입력에서는 score와 summary 중심으로 사용합니다.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## 분기 C escalate 사유",
                f"- 미달 항목 {len(b['failed'])}개: {', '.join(b['failed'])}",
                "- 권장: corpus scoring/brand matching 재처리 PoC 후 B-1 재개",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--strategic-brand", type=Path, required=True)
    parser.add_argument("--cd-brand", type=Path)
    parser.add_argument("--sample-output", type=Path, required=True)
    parser.add_argument("--metrics-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    args.sample_output.parent.mkdir(parents=True, exist_ok=True)
    catalog_names, _ = load_catalog_names(args.strategic_brand, args.cd_brand)
    samples = choose_sample(args.corpus, args.sample_size, args.seed)
    result = evaluate(samples, catalog_names)

    args.sample_output.write_text(json.dumps(result.samples, ensure_ascii=False, indent=2), encoding="utf-8")
    args.metrics_output.write_text(json.dumps(result.metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(result.metrics, args.markdown_output)
    print(json.dumps(result.metrics["branch_decision"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
