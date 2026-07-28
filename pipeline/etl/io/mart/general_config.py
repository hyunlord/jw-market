from __future__ import annotations

import os
from pathlib import Path

import pymysql

from pipeline.etl.lib.ops_utils import configure_logging, find_project_root, first_existing, retry

LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
OUTPUT_DIR = PROJECT_ROOT / "output"
CATALOG_DIR = Path(os.environ["S4_CATALOG_DIR"]) if os.environ.get("S4_CATALOG_DIR") else first_existing(OUTPUT_DIR / "catalog", PROJECT_ROOT / "parquet")
ENRICHED_DIR = Path(os.environ.get("S4_ENRICHED_DIR", str(OUTPUT_DIR / "enriched")))
IQVIA_NSA_DIR = Path(os.environ.get("S4_IQVIA_NSA_DIR", str(OUTPUT_DIR / "iqvia_nsa")))
UBIST_DIR = Path(os.environ.get("S4_UBIST_DIR", str(OUTPUT_DIR / "ubist")))
DRY_RUN_DIR = Path("/tmp")
ALLOWED_SOURCES = ("ubist", "iqvia_nsa")
GENERAL_HISTORY_YEARS = 5
UBIST_HISTORY_PERIODS = GENERAL_HISTORY_YEARS * 12
IQVIA_RETENTION_PERIODS = 24
IQVIA_CALCULATION_PERIODS = GENERAL_HISTORY_YEARS * 4 + 1
IQVIA_DISPLAY_PERIODS = GENERAL_HISTORY_YEARS * 4
# Compatibility alias for callers whose contract is the serving/display window.
IQVIA_HISTORY_PERIODS = IQVIA_DISPLAY_PERIODS
MEASURES_BY_SOURCE = {
    "ubist": ("sales", "volume"),
    "iqvia_nsa": ("sales", "unit", "dosage_unit", "counting_unit"),
}


def catalog_dir() -> Path:
    return Path(os.environ.get("S4_CATALOG_DIR", str(CATALOG_DIR)))


UNIT_LABELS = {
    ("ubist", "sales"): "KRW",
    ("ubist", "volume"): "Rx",
    ("iqvia_nsa", "sales"): "KRW",
    ("iqvia_nsa", "unit"): "unit",
    ("iqvia_nsa", "dosage_unit"): "dosage unit",
    ("iqvia_nsa", "counting_unit"): "counting unit",
}
GENERAL_BRAND_INSERT_COLUMNS = [
    "brand_key", "brand_name", "atc4_code", "atc4_desc", "source", "measure", "unit_label",
    "metric_history", "extended_metric_history", "channel_data", "specialty_data", "channel_specialty_matrix",
    "audit_code_matrix", "dimension_data", "dimension_channel_data", "by_dimension", "raw_value_history", "payload",
]
GENERAL_MARKET_INSERT_COLUMNS = [
    "atc4_code", "atc4_desc", "source", "measure", "unit_label", "market_size_series", "hhi_series",
    "brand_ranking", "company_ranking_stacked", "company_concentration_trend", "ei_ms_matrix",
    "growth_contribution_ms_matrix", "growth_contribution", "analysis_levels", "level_top5_trend",
    "target_customer_competition", "payload",
]
JSON_INSERT_COLUMNS = {
    "metric_history", "extended_metric_history", "channel_data", "specialty_data", "dimension_data",
    "channel_specialty_matrix", "audit_code_matrix", "dimension_channel_data", "dimension_specialty_data", "by_dimension", "raw_value_history",
    "market_size_series", "hhi_series", "hhi_series_5y", "brand_ranking", "brand_ranking_stacked",
    "company_ranking_stacked", "company_concentration_trend", "ei_ms_matrix", "growth_contribution_ms_matrix",
    "growth_contribution", "analysis_levels", "level_top5_trend", "target_customer_competition",
    "overlay_data", "cd_overlay", "payload", "ubist_channel_by_display", "ubist_channel_by_code",
}
SKU_DIMENSION_COLUMNS = ("nhi_type", "molecule", "dosage_form", "strength_pack", "ox_gx", "fish_oil")


def enriched_glob(ml: str | None = None) -> str:
    root = Path(os.environ.get("S4_ENRICHED_DIR", str(ENRICHED_DIR)))
    return str(root / f"ml_id={ml}" / "data.parquet") if ml else str(root / "ml_id=*" / "data.parquet")


def iqvia_nsa_glob() -> str:
    root = Path(os.environ.get("S4_IQVIA_NSA_DIR", str(IQVIA_NSA_DIR)))
    return str(root / "*.parquet")


def ubist_glob() -> str:
    root = Path(os.environ.get("S4_UBIST_DIR", str(UBIST_DIR)))
    return str(root / "year=*" / "month=*" / "data.parquet")


def general_brand_jsonl_path(source: str, output_dir: Path | None = None) -> Path:
    return (output_dir or DRY_RUN_DIR) / f"general_v3_{source}_brand_rows.jsonl"


def general_market_jsonl_path(source: str, output_dir: Path | None = None) -> Path:
    return (output_dir or DRY_RUN_DIR) / f"general_v3_{source}_market_rows.jsonl"

def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        # Stage 빌드/복구 환경에서는 pipeline/docker/.env가 없고 컨테이너가
        # MARIADB_* 환경변수만 제공하는 경우가 있다. 이때도 official script를
        # 그대로 쓰기 위해 env fallback을 허용한다. 별도 staging harness를
        # 만드는 대안은 운영 빌더 경로 검증을 흐리므로 기각했다.
        return {key: value for key, value in os.environ.items() if key.startswith("MARIADB_") or key == "HOST_PORT"}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    # .env를 읽은 뒤에도 shell env가 있으면 그 값을 우선한다.
    # 로컬 live와 staging schema를 같은 script로 오갈 때 필요한 override이며,
    # 파일을 직접 수정하는 방식은 보호 파일 drift를 만들기 때문에 기각했다.
    for key in ("MARIADB_HOST", "MARIADB_PORT", "MARIADB_DATABASE", "MARIADB_USER", "MARIADB_PASSWORD", "HOST_PORT"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env

def mariadb_connect(cursorclass=pymysql.cursors.DictCursor) -> pymysql.connections.Connection:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    if "MARIADB_PASSWORD" not in env:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {env_path}")
    return pymysql.connect(
        # HOST/PORT도 env로 열어 staging DB와 recover DB를 같은 코드 경로에서
        # 다룬다. host를 127.0.0.1로 고정하는 대안은 docker recover 환경에서
        # 접속 경로를 바꾸기 어렵게 해 기각했다.
        host=env.get("MARIADB_HOST", "127.0.0.1"),
        port=int(env.get("MARIADB_PORT") or env.get("HOST_PORT", "3307")),
        user=env.get("MARIADB_USER", "jwapp"),
        password=env["MARIADB_PASSWORD"],
        database=env.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=cursorclass,
    )
