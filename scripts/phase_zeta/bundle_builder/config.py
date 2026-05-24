from __future__ import annotations

from dataclasses import asdict, dataclass
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
    deduplication: dict | None = None
    brand_centric_classification: dict | None = None
    brand_centric_max_count: int = 15
    market_trend_max_count: int = 15


@dataclass(frozen=True)
class MarketViewSourceConfig:
    source: str
    measures: Tuple[str, ...]


@dataclass(frozen=True)
class MarketViewMatrixConfig:
    view: str
    sources: Tuple[MarketViewSourceConfig, ...]


@dataclass(frozen=True)
class MarketConfig:
    lookback_months: int
    include_mat_12m: bool = False
    brand_metrics: Tuple[tuple, ...] = ()
    include_market_size: bool = True
    include_hhi: bool = True
    include_mat_12m_absolute: bool = False
    views_matrix: Tuple[MarketViewMatrixConfig, ...] = ()
    ms_computation: dict | None = None
    atc4_source: str | None = None
    include_channel_breakdown: bool = False
    channel_filter: str = "전체"


@dataclass(frozen=True)
class CompetitorConfig:
    latest_n_months: int = 3
    include_mat_12m: bool = True
    recent_high_score_event_min: int = 70
    recent_high_score_event_max_count: int = 5
    selection_method: str = "catalog"
    top_n: int = 5
    per_source: bool = False
    events: dict | None = None


@dataclass(frozen=True)
class ForecastSimulationConfig:
    enabled: bool = False
    source: str = "cache_deep_analysis"
    fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class NumberFormatConfig:
    raw_value_display: str = "comma_separated"
    forbid_unit_conversion: bool = True
    decimal_places: int = 2
    percent_decimal_places: int = 2


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
    forecast_simulation: ForecastSimulationConfig = ForecastSimulationConfig()
    number_format: NumberFormatConfig = NumberFormatConfig()

    @classmethod
    def from_yaml(cls, path: str) -> "BundleConfig":
        config_path = Path(path)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        event_raw = raw["event"]
        event = EventConfig(
            lookback_months=int(event_raw["lookback_months"]),
            min_score_direct=int(event_raw["min_score_direct"]),
            max_count_direct=int(event_raw["max_count_direct"]),
            min_score_cross=int(event_raw["min_score_cross"]),
            max_count_cross=int(event_raw["max_count_cross"]),
            sort_order=tuple(SortRule(**rule) for rule in event_raw["sort_order"]),
            deduplication=event_raw.get("deduplication"),
            brand_centric_classification=event_raw.get("brand_centric_classification"),
            brand_centric_max_count=int(event_raw.get("brand_centric_max_count", 15)),
            market_trend_max_count=int(event_raw.get("market_trend_max_count", 15)),
        )

        brand_metrics = []
        market_raw = raw["market"]
        for item in market_raw.get("brand_metrics", []):
            for measure in item.get("measures", []):
                brand_metrics.append((item["source"], measure))
        views_matrix = []
        for view_item in market_raw.get("views_matrix", []):
            sources = []
            for source_item in view_item.get("sources", []):
                sources.append(
                    MarketViewSourceConfig(
                        source=str(source_item["source"]),
                        measures=tuple(str(measure) for measure in source_item.get("measures", [])),
                    )
                )
                for measure in source_item.get("measures", []):
                    pair = (str(source_item["source"]), str(measure))
                    if pair not in brand_metrics:
                        brand_metrics.append(pair)
            views_matrix.append(MarketViewMatrixConfig(view=str(view_item["view"]), sources=tuple(sources)))
        market = MarketConfig(
            lookback_months=int(market_raw["lookback_months"]),
            include_mat_12m=bool(market_raw.get("include_mat_12m", market_raw.get("include_mat_12m_absolute", False))),
            brand_metrics=tuple(brand_metrics),
            include_market_size=bool(market_raw["include_market_size"]),
            include_hhi=bool(market_raw["include_hhi"]),
            include_mat_12m_absolute=bool(market_raw.get("include_mat_12m_absolute", False)),
            views_matrix=tuple(views_matrix),
            ms_computation=market_raw.get("ms_computation"),
            atc4_source=market_raw.get("atc4_source"),
            include_channel_breakdown=bool(market_raw.get("include_channel_breakdown", False)),
            channel_filter=str(market_raw.get("channel_filter", "전체")),
        )

        competitor_raw = raw["competitor"]
        events_raw = competitor_raw.get("events", {})
        competitor = CompetitorConfig(
            latest_n_months=int(competitor_raw.get("latest_n_months", events_raw.get("lookback_months", 3))),
            include_mat_12m=bool(competitor_raw.get("include_mat_12m", True)),
            recent_high_score_event_min=int(
                competitor_raw.get("recent_high_score_event_min", events_raw.get("min_score", 70))
            ),
            recent_high_score_event_max_count=int(
                competitor_raw.get("recent_high_score_event_max_count", events_raw.get("max_count_per_competitor", 5))
            ),
            selection_method=str(competitor_raw.get("selection_method", "catalog")),
            top_n=int(competitor_raw.get("top_n", 5)),
            per_source=bool(competitor_raw.get("per_source", False)),
            events=events_raw or None,
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

        forecast_raw = raw.get("forecast_simulation", {})
        forecast = ForecastSimulationConfig(
            enabled=bool(forecast_raw.get("enabled", False)),
            source=str(forecast_raw.get("source", "cache_deep_analysis")),
            fields=tuple(str(field) for field in forecast_raw.get("fields", [])),
        )
        number_raw = raw.get("number_format", {})
        number_format = NumberFormatConfig(
            raw_value_display=str(number_raw.get("raw_value_display", "comma_separated")),
            forbid_unit_conversion=bool(number_raw.get("forbid_unit_conversion", True)),
            decimal_places=int(number_raw.get("decimal_places", 2)),
            percent_decimal_places=int(number_raw.get("percent_decimal_places", 2)),
        )

        return cls(
            config_version=str(raw["config_version"]),
            builder_version=str(raw["builder_version"]),
            event=event,
            market=market,
            competitor=competitor,
            db=db,
            pilot_brands=pilot_brands,
            forecast_simulation=forecast,
            number_format=number_format,
        )

    def to_dict_for_hash(self) -> dict:
        return {
            "config_version": self.config_version,
            "builder_version": self.builder_version,
            "event": asdict(self.event),
            "market": asdict(self.market),
            "competitor": asdict(self.competitor),
            "forecast_simulation": asdict(self.forecast_simulation),
            "number_format": asdict(self.number_format),
            "pilot_brands": list(self.pilot_brands),
        }
