from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from jw_chat_agent_poc.service.v4.contracts import SourceResult


FactKind = Literal["number", "period", "date", "identifier", "name"]
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_DATE_RE = re.compile(r"\b20\d{2}(?:-(?:0[1-9]|1[0-2])(?:-\d{2})?)?\b")
_IDENTIFIER_RE = re.compile(r"\b(?:NCT\d{8}|[A-Z]\d{2}(?:\.\d)?)\b", re.IGNORECASE)
_NAME_KEYS = frozenset(
    {
        "name",
        "brand",
        "product",
        "product_name",
        "item_name",
        "ingredient",
        "company",
        "manufacturer",
        "market_name",
        "disease_name",
    }
)
_DEFAULT_MAX_FACTS = 1_600


@dataclass(frozen=True)
class CanonicalFact:
    source: str
    path: str
    kind: FactKind
    canonical: str
    subject_grain: str


def build_canonical_ledger(
    results: Sequence[SourceResult],
    *,
    max_facts: int = _DEFAULT_MAX_FACTS,
) -> tuple[CanonicalFact, ...]:
    facts: list[CanonicalFact] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for result in results:
        if result.status != "ok":
            continue
        grain = result.evidence.subject_grain if result.evidence else "unknown"
        for path, value in _walk_scalars(result.payload):
            for kind, canonical in _canonical_values(path, value):
                fact_grain = _path_grain(path, grain)
                key = (result.source, path, kind, canonical, fact_grain)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(
                    CanonicalFact(
                        source=result.source,
                        path=path,
                        kind=kind,
                        canonical=canonical,
                        subject_grain=fact_grain,
                    )
                )
                if len(facts) >= max_facts:
                    return tuple(facts)
    return tuple(facts)


def build_grounding_shadow(
    answer: str,
    results: Sequence[SourceResult],
    *,
    ledger: Sequence[CanonicalFact] | None = None,
) -> dict[str, Any]:
    canonical_ledger = tuple(ledger or build_canonical_ledger(results))
    numeric_index: dict[str, list[CanonicalFact]] = {}
    for fact in canonical_ledger:
        if fact.kind == "number":
            numeric_index.setdefault(fact.canonical, []).append(fact)

    findings: list[dict[str, Any]] = []
    counts = Counter({"grounded": 0, "ungrounded": 0, "unknown": 0, "period": 0, "grain_mismatch": 0})
    for match in tuple(_NUMBER_RE.finditer(answer))[:256]:
        if _is_period_token(answer, match.start(), match.end()):
            counts["period"] += 1
            continue
        canonical = _canonical_number(match.group(0))
        sentence = _sentence_at(answer, match.start())
        if canonical is None:
            classification = "unknown"
            matched: list[CanonicalFact] = []
        else:
            matched = numeric_index.get(canonical, [])
            classification = "grounded" if matched else "ungrounded"
        counts[classification] += 1

        sentence_grain = _sentence_grain(sentence)
        evidence_grains = sorted(
            {fact.subject_grain for fact in matched if fact.subject_grain != "unknown"}
        )
        grain_mismatch = bool(
            sentence_grain != "unknown"
            and evidence_grains
            and sentence_grain not in evidence_grains
        )
        if grain_mismatch:
            counts["grain_mismatch"] += 1
        if classification != "grounded" or grain_mismatch:
            findings.append(
                {
                    "surface": match.group(0),
                    "canonical": canonical,
                    "classification": classification,
                    "sentence": sentence,
                    "sentence_grain": sentence_grain,
                    "evidence_grains": evidence_grains,
                    "grain_mismatch": grain_mismatch,
                    "matched_paths": [
                        f"{fact.source}:{fact.path}" for fact in matched[:5]
                    ],
                }
            )

    serialized_ledger = json.dumps(
        [asdict(fact) for fact in canonical_ledger],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "mode": "SHADOW_RECORD_ONLY",
        "answer_mutation": False,
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "counts": dict(counts),
        "findings": findings,
        "ledger": {
            "fact_count": len(canonical_ledger),
            "sha256": hashlib.sha256(serialized_ledger.encode()).hexdigest(),
            "by_source": dict(Counter(fact.source for fact in canonical_ledger)),
            "by_kind": dict(Counter(fact.kind for fact in canonical_ledger)),
            "truncated": len(canonical_ledger) >= _DEFAULT_MAX_FACTS,
        },
    }


def _canonical_values(path: str, value: Any) -> tuple[tuple[FactKind, str], ...]:
    if isinstance(value, bool) or value is None:
        return ()
    output: list[tuple[FactKind, str]] = []
    if isinstance(value, (int, float, Decimal)):
        canonical = _canonical_number(str(value))
        if canonical is not None:
            output.extend(
                ("number", variant)
                for variant in _numeric_display_variants(path, Decimal(canonical))
            )
        return tuple(output)

    text = str(value).strip()
    if not text or len(text) > 200:
        return ()
    if _DATE_RE.fullmatch(text):
        output.append(("period" if _is_period_path(path) else "date", text))
    if _IDENTIFIER_RE.fullmatch(text):
        output.append(("identifier", text.upper()))
    leaf = re.split(r"[.\[]", path)[-1].rstrip("]").casefold()
    if leaf in _NAME_KEYS and 1 < len(text) <= 100:
        output.append(("name", " ".join(text.casefold().split())))
    canonical_number = _canonical_number(text)
    if canonical_number is not None:
        output.extend(
            ("number", variant)
            for variant in _numeric_display_variants(path, Decimal(canonical_number))
        )
    return tuple(output)


def _numeric_display_variants(path: str, value: Decimal) -> tuple[str, ...]:
    """Return exact and deterministic public-display forms for one payload value."""

    lowered = path.casefold()
    variants = [_decimal_text(value)]
    money_krw = any(
        marker in lowered
        for marker in ("_krw", "krw_", "amount", "sales", "금액", "매출")
    )
    money_eok = any(marker in lowered for marker in ("억원", "_eok", "eok_"))
    if money_krw and abs(value) >= Decimal("10000000"):
        eok = value / Decimal("100000000")
        variants.append(_rounded_text(eok, Decimal("0.01")))
        if "market" in lowered or "시장" in lowered:
            variants.append(_rounded_text(eok, Decimal("1")))
    if money_eok:
        variants.append(_rounded_text(value, Decimal("0.01")))
        if "market" in lowered or "시장" in lowered:
            variants.append(_rounded_text(value, Decimal("1")))
    if any(marker in lowered for marker in ("prescription", "rx", "처방")) and abs(value) >= Decimal("10000"):
        variants.append(_rounded_text(value / Decimal("10000"), Decimal("1")))
    if any(marker in lowered for marker in ("pct", "percent", "share", "rate", "growth", "cagr")):
        variants.append(_rounded_text(value, Decimal("0.01")))
    return tuple(dict.fromkeys(variants))


def _path_grain(path: str, fallback: str) -> str:
    lowered = path.casefold()
    for marker, grain in (
        ("specialty", "specialty"),
        ("진료과", "specialty"),
        ("channel", "channel"),
        ("채널", "channel"),
        ("company", "company"),
        ("회사", "company"),
        ("market", "market"),
        ("시장", "market"),
        ("brand", "brand"),
        ("product", "brand"),
        ("브랜드", "brand"),
    ):
        if marker in lowered:
            return grain
    return fallback


def _rounded_text(value: Decimal, quantum: Decimal) -> str:
    return _decimal_text(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _walk_scalars(value: Any, prefix: str = ""):
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_scalars(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_scalars(item, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _canonical_number(value: str) -> str | None:
    cleaned = value.replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    try:
        return _decimal_text(Decimal(cleaned))
    except InvalidOperation:
        return None


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _sentence_at(text: str, offset: int) -> str:
    start = max(text.rfind(".", 0, offset), text.rfind("\n", 0, offset)) + 1
    end_candidates = [index for index in (text.find(".", offset), text.find("\n", offset)) if index >= 0]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    return " ".join(text[start:end].split())


def _is_period_token(text: str, start: int, end: int) -> bool:
    token = text[start:end].replace(",", "")
    suffix = text[end : end + 3]
    if re.fullmatch(r"20\d{2}", token):
        if suffix.startswith(("년", "-")):
            return True
        surrounding = text[max(0, start - 6) : min(len(text), end + 6)]
        if re.search(r"20\d{2}\s*[~～-]\s*20\d{2}년?", surrounding):
            return True
    if suffix.startswith(("년", "월", "분기")):
        return True
    return False


def _is_period_path(path: str) -> bool:
    leaf = re.split(r"[.\[]", path)[-1].rstrip("]").casefold()
    return any(marker in leaf for marker in ("period", "year", "month", "quarter", "yyyymm"))


def _sentence_grain(sentence: str) -> str:
    lowered = sentence.casefold()
    for marker, grain in (
        ("진료과", "specialty"),
        ("채널", "channel"),
        ("성분", "ingredient"),
        ("회사", "company"),
        ("시장", "market"),
        ("브랜드", "brand"),
    ):
        if marker in lowered:
            return grain
    return "unknown"
