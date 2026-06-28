from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BrandRecord:
    """One mart brand row parsed from mart_strategic_ml_brand_metric."""

    brand_name: str
    company: str
    manufacturer: str
    molecule: str
    class_label: str
    ox_gx: str
    metric_history: dict[str, dict[str, Any]]
    channel_data: dict[str, dict[str, dict[str, float]]]
    specialty_data: dict[str, dict[str, dict[str, float]]]
    dimension_data: dict[str, Any]


class MartStore:
    """Read-only mart snapshot with deterministic market calculations."""

    def __init__(self, records: tuple[BrandRecord, ...]) -> None:
        self.records = records
        self.by_brand = {record.brand_name: record for record in records}
        periods: set[str] = set()
        for record in records:
            periods.update(record.metric_history)
        self.periods = tuple(sorted(periods))
        self.recent_period = self.periods[-1]

    @classmethod
    def from_tsv(cls, path: Path) -> "MartStore":
        records: list[BrandRecord] = []
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                dim = _loads(row["by_dimension"])
                records.append(
                    BrandRecord(
                        brand_name=row["brand_name"],
                        company=str(dim.get("company") or dim.get("raw_company") or ""),
                        manufacturer=str(dim.get("manufacturer") or ""),
                        molecule=str(dim.get("molecule") or "unknown"),
                        class_label=str(dim.get("class") or "unknown"),
                        ox_gx=str(dim.get("ox_gx") or "unknown"),
                        metric_history=_loads(row["metric_history"]),
                        channel_data=_loads(row["channel_data"]),
                        specialty_data=_loads(row["specialty_data"]),
                        dimension_data=_loads(row["dimension_data"]),
                    )
                )
        return cls(tuple(records))

    def last_periods(self, n: int) -> tuple[str, ...]:
        return self.periods[-n:]

    def record(self, brand: str) -> BrandRecord:
        return self.by_brand[brand]

    def value(self, brand: str, period: str) -> float:
        return _num(self.record(brand).metric_history.get(period, {}).get("raw_value"))

    def share(self, brand: str, period: str) -> float:
        return _num(self.record(brand).metric_history.get(period, {}).get("ms"))

    def rank(self, brand: str, period: str) -> int | None:
        value = self.record(brand).metric_history.get(period, {}).get("rank")
        return int(value) if value is not None else None

    def market_value(self, period: str) -> float:
        return sum(_num(record.metric_history.get(period, {}).get("raw_value")) for record in self.records)

    def top_brands(self, n: int, period: str | None = None, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        p = period or self.recent_period
        excluded = exclude or set()
        rows = [
            {
                "brand": record.brand_name,
                "value": _num(record.metric_history.get(p, {}).get("raw_value")),
                "share_pct": _num(record.metric_history.get(p, {}).get("ms")),
                "rank": record.metric_history.get(p, {}).get("rank"),
                "company": record.company,
                "molecule": record.molecule,
            }
            for record in self.records
            if record.brand_name not in excluded
        ]
        return sorted(rows, key=lambda item: item["value"], reverse=True)[:n]

    def hhi(self, period: str | None = None) -> float:
        p = period or self.recent_period
        return sum(self.share(record.brand_name, p) ** 2 for record in self.records)

    def market_series(self, periods: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{"period": period, "value": self.market_value(period)} for period in periods]

    def brand_series(self, brand: str, periods: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            {
                "period": period,
                "value": self.value(brand, period),
                "share_pct": self.share(brand, period),
                "rank": self.rank(brand, period),
            }
            for period in periods
        ]

    def channel_molecule_share(self, channel: str, period: str | None = None) -> list[dict[str, Any]]:
        p = period or self.recent_period
        grouped: dict[str, float] = {}
        for record in self.records:
            value = _period_value(record.channel_data.get(channel, {}), p)
            grouped[record.molecule] = grouped.get(record.molecule, 0.0) + value
        total = sum(grouped.values())
        return _ranked_share_rows(grouped, total, "molecule")

    def channel_brand_shares(self, brands: tuple[str, ...], period: str | None = None) -> list[dict[str, Any]]:
        p = period or self.recent_period
        rows: list[dict[str, Any]] = []
        channels = sorted({channel for record in self.records for channel in record.channel_data})
        for channel in channels:
            market_total = sum(_period_value(record.channel_data.get(channel, {}), p) for record in self.records)
            item: dict[str, Any] = {"channel": channel, "market_value": market_total}
            for brand in brands:
                value = _period_value(self.record(brand).channel_data.get(channel, {}), p)
                item[f"{brand}_value"] = value
                item[f"{brand}_share_pct"] = value / market_total * 100 if market_total else 0.0
            rows.append(item)
        return rows

    def ox_gx_mix(self, period: str | None = None) -> list[dict[str, Any]]:
        p = period or self.recent_period
        grouped: dict[str, float] = {}
        for record in self.records:
            value = _period_value(record.dimension_data.get("ox_gx", {}).get(record.ox_gx, {}), p)
            if not value:
                value = self.value(record.brand_name, p)
            grouped[record.ox_gx] = grouped.get(record.ox_gx, 0.0) + value
        return _ranked_share_rows(grouped, sum(grouped.values()), "segment")

    def specialty_sales(self, brands: tuple[str, ...], period: str | None = None, top_n: int = 5) -> dict[str, list[dict[str, Any]]]:
        p = period or self.recent_period
        result: dict[str, list[dict[str, Any]]] = {}
        for brand in brands:
            record = self.record(brand)
            rows = [
                {"specialty": name, "value": _period_value(history, p)}
                for name, history in record.specialty_data.items()
            ]
            result[brand] = sorted(rows, key=lambda item: item["value"], reverse=True)[:top_n]
        return result

    def group_series(self, attr: str, periods: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for period in periods:
            grouped: dict[str, float] = {}
            for record in self.records:
                key = getattr(record, attr)
                grouped[key] = grouped.get(key, 0.0) + self.value(record.brand_name, period)
            for key, value in grouped.items():
                result.setdefault(key, []).append({"period": period, "value": value})
        return result

    def top_company_molecules(self, n: int, period: str | None = None) -> list[dict[str, Any]]:
        p = period or self.recent_period
        company_values: dict[str, float] = {}
        company_molecules: dict[str, dict[str, float]] = {}
        for record in self.records:
            value = self.value(record.brand_name, p)
            company_values[record.company] = company_values.get(record.company, 0.0) + value
            by_molecule = company_molecules.setdefault(record.company, {})
            by_molecule[record.molecule] = by_molecule.get(record.molecule, 0.0) + value
        companies = sorted(company_values.items(), key=lambda item: item[1], reverse=True)[:n]
        return [
            {
                "company": company,
                "value": value,
                "molecules": _ranked_share_rows(company_molecules[company], value, "molecule")[:3],
            }
            for company, value in companies
        ]


def _loads(value: str) -> dict[str, Any]:
    parsed = json.loads(value) if value else {}
    return parsed if isinstance(parsed, dict) else {}


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and value is not None else 0.0


def _period_value(history: dict[str, Any], period: str) -> float:
    item = history.get(period, {})
    return _num(item.get("raw_value")) if isinstance(item, dict) else 0.0


def _ranked_share_rows(values: dict[str, float], total: float, key_name: str) -> list[dict[str, Any]]:
    rows = [
        {key_name: key, "value": value, "share_pct": value / total * 100 if total else 0.0}
        for key, value in values.items()
    ]
    rows.sort(key=lambda item: item["value"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows

