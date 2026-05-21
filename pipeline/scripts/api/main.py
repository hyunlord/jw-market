from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import FastAPI


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.config import config  # noqa: E402
from pipeline.scripts.api.db import close_pool, init_pool  # noqa: E402
from pipeline.scripts.api.routes import brands, cause, deep_analysis, health, market_status  # noqa: E402


logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    logger.info(
        "JW Market API starting: version=%s prefix=%s db=%s:%s/%s",
        config.app_version,
        config.external_path_prefix,
        config.db_host,
        config.db_port,
        config.db_name,
    )
    yield
    close_pool()


app = FastAPI(
    title="JW Market Analysis API",
    version=config.app_version,
    root_path=config.external_path_prefix,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(brands.router)
app.include_router(market_status.router)
app.include_router(cause.router)
app.include_router(deep_analysis.router)
