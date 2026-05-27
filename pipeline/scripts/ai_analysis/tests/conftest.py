import os
import sys
from datetime import timezone, timedelta
from pathlib import Path

import pymysql
import pytest

PHASE_ZETA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PHASE_ZETA_ROOT.parents[2]
if str(PHASE_ZETA_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE_ZETA_ROOT))

KST = timezone(timedelta(hours=9))


@pytest.fixture(scope="session")
def config_path():
    return PHASE_ZETA_ROOT / "configs" / "phase_zeta_v1.yaml"


@pytest.fixture(scope="session")
def config_v1_1_path():
    return PHASE_ZETA_ROOT / "configs" / "phase_zeta_v1_1.yaml"


@pytest.fixture(scope="session")
def config(config_path):
    from bundle_builder import BundleConfig

    return BundleConfig.from_yaml(str(config_path))


@pytest.fixture(scope="session")
def config_v1_1(config_v1_1_path):
    from bundle_builder import BundleConfig

    return BundleConfig.from_yaml(str(config_v1_1_path))


@pytest.fixture(scope="session")
def db_conn(config):
    conn = pymysql.connect(
        host=config.db.host,
        port=config.db.port,
        user=os.environ.get(config.db.user_env, "root"),
        password=os.environ.get(config.db.password_env, ""),
        database=config.db.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    yield conn
    conn.close()
