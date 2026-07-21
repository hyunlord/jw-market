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
    # 이 픽스처를 쓰는 테스트(test_determinism.py, test_v1_1_schema.py)는
    # 라이브 MySQL에 붙어 번들을 실제로 조립·검증하는 통합(integration) 테스트다.
    # DB가 없는 순수 단위테스트 환경에서는 연결 실패로 ERROR가 나므로,
    # 연결 불가 시 skip 처리한다(단언 약화·삭제 아님, 실행 환경 게이팅).
    # DB가 준비된 환경에서는 종전과 동일하게 정상 실행된다.
    try:
        conn = pymysql.connect(
            host=config.db.host,
            port=config.db.port,
            user=os.environ.get(config.db.user_env, "root"),
            password=os.environ.get(config.db.password_env, ""),
            database=config.db.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.err.MySQLError as exc:
        pytest.skip(f"DB 연결 불가로 통합 테스트 skip (환경 의존): {exc}")
    yield conn
    conn.close()
