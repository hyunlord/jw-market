from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import yaml


@dataclass(frozen=True)
class SortRule:
    field: str
    direction: str


@dataclass(frozen=True)
class EventConfig:
    lookback_months: int
    min_score_direct: int
    max_count_direct: int
    min_score_cross: int
    max_count_cross: int
    sort_order: Tuple[SortRule, ...]


@dataclass(frozen=True)
class MarketConfig:
    lookback_months: int
    include_mat_12m: bool
    brand_metrics: Tuple[tuple, ...]
    include_market_size: bool
    include_hhi: bool


@dataclass(frozen=True)
class CompetitorConfig:
    latest_n_months: int
    include_mat_12m: bool
    recent_high_score_event_min: int
    recent_high_score_event_max_count: int


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user_env: str
    password_env: str
    database: str


@dataclass(frozen=True)
class BundleConfig:
    config_version: str
    builder_version: str
    event: EventConfig
    market: MarketConfig
    competitor: CompetitorConfig
    db: DbConfig
    pilot_brands: tuple

    @classmethod
    def from_yaml(cls, path: str) -> "BundleConfig":
        config_path = Path(path)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        event = EventConfig(
            lookback_months=int(raw["event"]["lookback_months"]),
            min_score_direct=int(raw["event"]["min_score_direct"]),
            max_count_direct=int(raw["event"]["max_count_direct"]),
            min_score_cross=int(raw["event"]["min_score_cross"]),
            max_count_cross=int(raw["event"]["max_count_cross"]),
            sort_order=tuple(SortRule(**rule) for rule in raw["event"]["sort_order"]),
        )

        brand_metrics = []
        for item in raw["market"]["brand_metrics"]:
            for measure in item["measures"]:
                brand_metrics.append((item["source"], measure))
        market = MarketConfig(
            lookback_months=int(raw["market"]["lookback_months"]),
            include_mat_12m=bool(raw["market"]["include_mat_12m"]),
            brand_metrics=tuple(brand_metrics),
            include_market_size=bool(raw["market"]["include_market_size"]),
            include_hhi=bool(raw["market"]["include_hhi"]),
        )

        competitor = CompetitorConfig(
            latest_n_months=int(raw["competitor"]["latest_n_months"]),
            include_mat_12m=bool(raw["competitor"]["include_mat_12m"]),
            recent_high_score_event_min=int(raw["competitor"]["recent_high_score_event_min"]),
            recent_high_score_event_max_count=int(raw["competitor"]["recent_high_score_event_max_count"]),
        )

        db_raw = raw["db"]
        db = DbConfig(
            host=str(db_raw["host"]),
            port=int(db_raw["port"]),
            user_env=str(db_raw["user_env"]),
            password_env=str(db_raw["password_env"]),
            database=str(db_raw["database"]),
        )

        pilot_path = config_path.parent / raw["pilot_brands_file"]
        pilot_brands = tuple(
            line.strip()
            for line in pilot_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

        return cls(
            config_version=str(raw["config_version"]),
            builder_version=str(raw["builder_version"]),
            event=event,
            market=market,
            competitor=competitor,
            db=db,
            pilot_brands=pilot_brands,
        )

    def to_dict_for_hash(self) -> dict:
        return {
            "config_version": self.config_version,
            "builder_version": self.builder_version,
            "event": {
                "lookback_months": self.event.lookback_months,
                "min_score_direct": self.event.min_score_direct,
                "max_count_direct": self.event.max_count_direct,
                "min_score_cross": self.event.min_score_cross,
                "max_count_cross": self.event.max_count_cross,
                "sort_order": [rule.__dict__ for rule in self.event.sort_order],
            },
            "market": {
                "lookback_months": self.market.lookback_months,
                "include_mat_12m": self.market.include_mat_12m,
                "brand_metrics": list(self.market.brand_metrics),
                "include_market_size": self.market.include_market_size,
                "include_hhi": self.market.include_hhi,
            },
            "competitor": {
                "latest_n_months": self.competitor.latest_n_months,
                "include_mat_12m": self.competitor.include_mat_12m,
                "recent_high_score_event_min": self.competitor.recent_high_score_event_min,
                "recent_high_score_event_max_count": self.competitor.recent_high_score_event_max_count,
            },
            "pilot_brands": list(self.pilot_brands),
        }
