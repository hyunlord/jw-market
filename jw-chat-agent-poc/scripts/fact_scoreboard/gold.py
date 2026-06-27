from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.compose_ab_poc.questions import QUESTIONS, EvalQuestion
from scripts.fact_scoreboard.scoring import GoldFact
from scripts.fact_scoreboard.text_numbers import NumericUnit

# allow: SIZE_OK — fixed 20-question gold formula registry stays together for audit traceability.


@dataclass(frozen=True, slots=True)
class MartRow:
    """Direct-SQL mart row used for independent gold calculations."""

    brand: str
    metric_history: dict[str, Any]
    channel_data: dict[str, Any]
    specialty_data: dict[str, Any]
    dimension_data: dict[str, Any]
    by_dimension: dict[str, Any]

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> "MartRow":
        return cls(
            brand=str(item["brand_name"]),
            metric_history=_loads(item.get("metric_history")),
            channel_data=_loads(item.get("channel_data")),
            specialty_data=_loads(item.get("specialty_data")),
            dimension_data=_loads(item.get("dimension_data")),
            by_dimension=_loads(item.get("by_dimension")),
        )


@dataclass(frozen=True, slots=True)
class GoldSet:
    """All ground-truth facts for one evaluation question."""

    question: EvalQuestion
    status: str
    facts: tuple[GoldFact, ...]
    notes: str


class GoldStore:
    """Independent mart calculator for the fixed 20-question evaluation set."""

    def __init__(self, rows: tuple[MartRow, ...]) -> None:
        self.rows = rows
        self.by_brand = {row.brand: row for row in rows}
        self.periods = tuple(sorted({period for row in rows for period in row.metric_history}))
        self.latest = self.periods[-1]

    @classmethod
    def from_jsonl(cls, path: Path) -> "GoldStore":
        rows = tuple(MartRow.from_json(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        return cls(rows)

    def facts_for_all(self) -> tuple[GoldSet, ...]:
        return tuple(self.facts_for(question) for question in QUESTIONS)

    def facts_for(self, question: EvalQuestion) -> GoldSet:
        match question.intent_id:
            case "brand_pair_sales_trend":
                facts = self._brand_series_facts(question.qid, ("리바로", "리바로젯"), self._last(6))
            case "top_share_trend":
                facts = self._top_brand_series_facts(question.qid, 3, self._last(10))
            case "share_decline_context":
                facts = self._brand_series_facts(question.qid, ("리바로",), self._last(10)) + self._top_snapshot_facts(question.qid, 5)
            case "market_vs_brand_feb":
                facts = self._market_vs_brand_facts(question.qid, "리바로", ("2026-01", "2026-02", "2026-03"))
            case "competition_change":
                facts = self._top_brand_series_facts(question.qid, 5, self._last(10))
            case "atozet_threat" | "atozet_livaro_cross_trend":
                periods = self._last(10)
                facts = self._brand_series_facts(question.qid, ("리바로", "아토젯"), periods) + self._brand_pair_delta_facts(question.qid, "리바로", "아토젯", periods)
            case "news_sales_effect":
                facts = self._brand_series_facts(question.qid, ("리바로",), self._last(6))
            case "livaro_yoy_growth":
                facts = self._yoy_facts(question.qid, "리바로")
            case "livaro_avg_share_6m":
                facts = self._avg_share_facts(question.qid, "리바로", self._last(6))
            case "market_concentration":
                facts = self._concentration_facts(question.qid)
            case "top5_share_sum":
                facts = self._top_snapshot_facts(question.qid, 5) + (self._fact(question.qid, "top5 share sum", sum(row["share"] for row in self._ranked(self.latest)[:5]), "percent"),)
            case "target_share_gap":
                facts = self._target_share_gap(question.qid, "리바로", 4.0)
            case "clinic_channel_molecule_share":
                facts = self._channel_molecule_facts(question.qid)
            case "livaro_atozet_channel_diff":
                facts = self._channel_brand_diff_facts(question.qid, "리바로", "아토젯")
            case "ox_gx_mix":
                facts = self._dimension_mix_facts(question.qid, "ox_gx")
            case "top_competitor_specialty_sales":
                facts = self._specialty_facts(question.qid, "리바로")
            case "class_sales_trend_12m":
                facts = self._group_series_facts(question.qid, "class", self._last(12))
            case "top_company_molecule":
                facts = self._company_molecule_facts(question.qid)
            case "nhi_mix_trend":
                if not any("nhi_type" in row.by_dimension or "nhi_type" in row.dimension_data for row in self.rows):
                    return GoldSet(question, "unsupported", (), "nhi_type dimension absent in mart rows")
                facts = self._dimension_mix_facts(question.qid, "nhi_type")
            case unreachable:
                raise AssertionError(f"unknown intent: {unreachable}")
        facts = facts + (self._fact(question.qid, f"market {self.latest} sales", self._eok(self._market(self.latest)), "eok", False),)
        return GoldSet(question, "ok", facts, "direct mart SQL snapshot")

    def _brand_series_facts(self, qid: str, brands: tuple[str, ...], periods: tuple[str, ...]) -> tuple[GoldFact, ...]:
        facts: list[GoldFact] = []
        for brand in brands:
            for period in periods:
                facts.append(self._fact(qid, f"{brand} {period} sales", self._eok(self._value(brand, period)), "eok", period == periods[-1]))
                facts.append(self._fact(qid, f"{brand} {period} share", self._share(brand, period), "percent", period == periods[-1]))
                rank = self._rank(brand, period)
                if rank is not None:
                    facts.append(self._fact(qid, f"{brand} {period} rank", float(rank), "rank", period == periods[-1]))
        return tuple(facts)

    def _top_brand_series_facts(self, qid: str, n: int, periods: tuple[str, ...]) -> tuple[GoldFact, ...]:
        brands = tuple(row["brand"] for row in self._ranked(self.latest)[:n])
        return self._brand_series_facts(qid, brands, periods)

    def _top_snapshot_facts(self, qid: str, n: int) -> tuple[GoldFact, ...]:
        facts: list[GoldFact] = []
        for row in self._ranked(self.latest)[:n]:
            brand = str(row["brand"])
            facts.append(self._fact(qid, f"{brand} latest share", float(row["share"]), "percent"))
            facts.append(self._fact(qid, f"{brand} latest sales", self._eok(float(row["value"])), "eok"))
            facts.append(self._fact(qid, f"{brand} latest rank", float(row["rank"]), "rank"))
        return tuple(facts)

    def _market_vs_brand_facts(self, qid: str, brand: str, periods: tuple[str, ...]) -> tuple[GoldFact, ...]:
        facts = list(self._brand_series_facts(qid, (brand,), periods))
        for period in periods:
            facts.append(self._fact(qid, f"market {period} sales", self._eok(self._market(period)), "eok", period == "2026-02"))
        facts.append(self._fact(qid, "brand Jan-Feb sales delta", self._eok(self._value(brand, "2026-02") - self._value(brand, "2026-01")), "eok"))
        facts.append(self._fact(qid, "market Jan-Feb sales delta", self._eok(self._market("2026-02") - self._market("2026-01")), "eok"))
        brand_start = self._value(brand, "2026-01")
        market_start = self._market("2026-01")
        brand_pct = (self._value(brand, "2026-02") / brand_start - 1) * 100 if brand_start else 0.0
        market_pct = (self._market("2026-02") / market_start - 1) * 100 if market_start else 0.0
        facts.append(self._fact(qid, "brand Jan-Feb sales pct change", brand_pct, "percent"))
        facts.append(self._fact(qid, "market Jan-Feb sales pct change", market_pct, "percent"))
        facts.append(self._fact(qid, "brand-market Jan-Feb pct gap", brand_pct - market_pct, "percent"))
        return tuple(facts)

    def _brand_pair_delta_facts(self, qid: str, brand: str, comparison: str, periods: tuple[str, ...]) -> tuple[GoldFact, ...]:
        if len(periods) < 2:
            return ()
        start, end = periods[0], periods[-1]
        brand_start = self._value(brand, start)
        comparison_start = self._value(comparison, start)
        return (
            self._fact(qid, f"trend_compare {brand} share delta", self._share(brand, end) - self._share(brand, start), "percent"),
            self._fact(qid, f"trend_compare {comparison} share delta", self._share(comparison, end) - self._share(comparison, start), "percent"),
            self._fact(qid, f"trend_compare {brand} sales pct", (self._value(brand, end) / brand_start - 1) * 100 if brand_start else 0.0, "percent"),
            self._fact(qid, f"trend_compare {comparison} sales pct", (self._value(comparison, end) / comparison_start - 1) * 100 if comparison_start else 0.0, "percent"),
        )

    def _yoy_facts(self, qid: str, brand: str) -> tuple[GoldFact, ...]:
        prior = f"{int(self.latest[:4]) - 1}{self.latest[4:]}"
        current = self._value(brand, self.latest)
        base = self._value(brand, prior)
        growth = (current / base - 1) * 100 if base else 0.0
        return (
            self._fact(qid, f"{brand} {self.latest} sales", self._eok(current), "eok"),
            self._fact(qid, f"{brand} {prior} sales", self._eok(base), "eok"),
            self._fact(qid, f"{brand} yoy growth", growth, "percent"),
        )

    def _avg_share_facts(self, qid: str, brand: str, periods: tuple[str, ...]) -> tuple[GoldFact, ...]:
        avg = sum(self._share(brand, period) for period in periods) / len(periods)
        return self._brand_series_facts(qid, (brand,), periods) + (self._fact(qid, f"{brand} avg share", avg, "percent"),)

    def _concentration_facts(self, qid: str) -> tuple[GoldFact, ...]:
        ranked = self._ranked(self.latest)
        return (
            self._fact(qid, "HHI", sum(float(row["share"]) ** 2 for row in ranked), "plain"),
            self._fact(qid, "top3 share", sum(float(row["share"]) for row in ranked[:3]), "percent"),
            self._fact(qid, "top5 share", sum(float(row["share"]) for row in ranked[:5]), "percent"),
            self._fact(qid, "brand count", float(len(ranked)), "count", False),
        )

    def _target_share_gap(self, qid: str, brand: str, target_share: float) -> tuple[GoldFact, ...]:
        market = self._market(self.latest)
        target_sales = market * target_share / 100
        current = self._value(brand, self.latest)
        return (
            self._fact(qid, f"{brand} current share", self._share(brand, self.latest), "percent"),
            self._fact(qid, "target share", target_share, "percent"),
            self._fact(qid, "target sales", self._eok(target_sales), "eok"),
            self._fact(qid, "sales gap", self._eok(target_sales - current), "eok"),
        )

    def _channel_molecule_facts(self, qid: str) -> tuple[GoldFact, ...]:
        channel = self._channel("의원")
        grouped: dict[str, float] = {}
        for row in self.rows:
            molecule = str(row.by_dimension.get("molecule") or "unknown")
            grouped[molecule] = grouped.get(molecule, 0.0) + _period_value(_nested(row.channel_data, channel), self.latest)
        return self._share_rows(qid, "molecule", grouped, 10)

    def _channel_brand_diff_facts(self, qid: str, left: str, right: str) -> tuple[GoldFact, ...]:
        facts: list[GoldFact] = []
        for channel in self._channels()[:8]:
            total = sum(_period_value(_nested(row.channel_data, channel), self.latest) for row in self.rows)
            left_share = _period_value(_nested(self.by_brand[left].channel_data, channel), self.latest) / total * 100 if total else 0.0
            right_share = _period_value(_nested(self.by_brand[right].channel_data, channel), self.latest) / total * 100 if total else 0.0
            facts.append(self._fact(qid, f"{channel} {left} share", left_share, "percent", False))
            facts.append(self._fact(qid, f"{channel} {right} share", right_share, "percent", False))
            facts.append(self._fact(qid, f"{channel} share diff", left_share - right_share, "percent", False))
        return tuple(facts)

    def _dimension_mix_facts(self, qid: str, key: str) -> tuple[GoldFact, ...]:
        grouped: dict[str, float] = {}
        for row in self.rows:
            value = str(row.by_dimension.get(key) or "unknown")
            grouped[value] = grouped.get(value, 0.0) + self._value(row.brand, self.latest)
        return self._share_rows(qid, key, grouped, 10)

    def _specialty_facts(self, qid: str, anchor: str) -> tuple[GoldFact, ...]:
        facts: list[GoldFact] = []
        competitors = tuple(str(row["brand"]) for row in self._ranked(self.latest) if row["brand"] != anchor)[:3]
        for brand in competitors:
            specialties = sorted(
                ((name, _period_value(history, self.latest)) for name, history in self.by_brand[brand].specialty_data.items()),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
            for name, value in specialties:
                facts.append(self._fact(qid, f"{brand} {name} sales", self._eok(value), "eok", False))
        return tuple(facts)

    def _group_series_facts(self, qid: str, dim_key: str, periods: tuple[str, ...]) -> tuple[GoldFact, ...]:
        facts: list[GoldFact] = []
        top_groups = [row[0] for row in sorted(self._group_values(dim_key, self.latest).items(), key=lambda item: item[1], reverse=True)[:5]]
        for period in periods:
            grouped = self._group_values(dim_key, period)
            for group in top_groups:
                facts.append(self._fact(qid, f"{dim_key} {group} {period} sales", self._eok(grouped.get(group, 0.0)), "eok", period == periods[-1]))
        return tuple(facts)

    def _company_molecule_facts(self, qid: str) -> tuple[GoldFact, ...]:
        company_values = self._group_values("company", self.latest)
        facts: list[GoldFact] = []
        for rank, (company, value) in enumerate(sorted(company_values.items(), key=lambda item: item[1], reverse=True)[:3], start=1):
            facts.append(self._fact(qid, f"{company} company rank", float(rank), "rank"))
            facts.append(self._fact(qid, f"{company} company sales", self._eok(value), "eok"))
            molecules: dict[str, float] = {}
            for row in self.rows:
                if str(row.by_dimension.get("company") or row.by_dimension.get("raw_company") or "") == company:
                    molecule = str(row.by_dimension.get("molecule") or "unknown")
                    molecules[molecule] = molecules.get(molecule, 0.0) + self._value(row.brand, self.latest)
            if molecules:
                top_molecule, top_value = sorted(molecules.items(), key=lambda item: item[1], reverse=True)[0]
                facts.append(self._fact(qid, f"{company} top molecule {top_molecule}", self._eok(top_value), "eok", False))
        return tuple(facts)

    def _share_rows(self, qid: str, prefix: str, grouped: dict[str, float], limit: int) -> tuple[GoldFact, ...]:
        total = sum(grouped.values())
        facts: list[GoldFact] = []
        for rank, (name, value) in enumerate(sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:limit], start=1):
            share = value / total * 100 if total else 0.0
            facts.append(self._fact(qid, f"{prefix} {name} rank", float(rank), "rank", False))
            facts.append(self._fact(qid, f"{prefix} {name} sales", self._eok(value), "eok", False))
            facts.append(self._fact(qid, f"{prefix} {name} share", share, "percent", False))
        return tuple(facts)

    def _group_values(self, key: str, period: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for row in self.rows:
            if key == "company":
                group = str(row.by_dimension.get("company") or row.by_dimension.get("raw_company") or "unknown")
            else:
                group = str(row.by_dimension.get(key) or "unknown")
            values[group] = values.get(group, 0.0) + self._value(row.brand, period)
        return values

    def _ranked(self, period: str) -> list[dict[str, str | float | int]]:
        rows = [{"brand": row.brand, "value": self._value(row.brand, period), "share": self._share(row.brand, period)} for row in self.rows]
        rows.sort(key=lambda item: float(item["value"]), reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows

    def _last(self, count: int) -> tuple[str, ...]:
        return self.periods[-count:]

    def _channels(self) -> tuple[str, ...]:
        return tuple(sorted({key for row in self.rows for key in row.channel_data}))

    def _channel(self, needle: str) -> str:
        channels = self._channels()
        if needle in channels:
            return needle
        for channel in channels:
            if needle in channel:
                return channel
        return channels[0] if channels else ""

    def _value(self, brand: str, period: str) -> float:
        return _num(_nested(self.by_brand[brand].metric_history, period).get("raw_value"))

    def _share(self, brand: str, period: str) -> float:
        return _num(_nested(self.by_brand[brand].metric_history, period).get("ms"))

    def _rank(self, brand: str, period: str) -> int | None:
        value = _nested(self.by_brand[brand].metric_history, period).get("rank")
        return int(value) if isinstance(value, int | float) else None

    def _market(self, period: str) -> float:
        return sum(self._value(row.brand, period) for row in self.rows)

    def _fact(self, qid: str, label: str, value: float, unit: NumericUnit, required: bool = True) -> GoldFact:
        return GoldFact(f"{qid}:{label}", label, value, unit, qid, required)

    @staticmethod
    def _eok(value: float) -> float:
        return value / 100_000_000


def load_gold_sets(path: Path) -> tuple[GoldSet, ...]:
    """Load mart JSONL and calculate the fixed evaluation set ground truth."""

    return GoldStore.from_jsonl(path).facts_for_all()


def _loads(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) and value else value
    return parsed if isinstance(parsed, dict) else {}


def _nested(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _period_value(history: dict[str, Any], period: str) -> float:
    return _num(_nested(history, period).get("raw_value"))


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0
