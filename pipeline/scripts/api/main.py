from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.config import config  # noqa: E402
from pipeline.scripts.api.db import close_pool, init_pool  # noqa: E402
from pipeline.scripts.api.routes import brand_activity, brands, cause, deep_analysis, dynamic_market, health, market_scope, market_status  # noqa: E402


logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


FRONTEND_FILENAME = "jw_market_hardcoded_mockup_v3_4.html"
FRONTEND_DIR = Path("/app/static")
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = Path(__file__).resolve().parents[3] / "docs" / "reference"
FRONTEND_FILE = FRONTEND_DIR / FRONTEND_FILENAME


def _prefix_path(path: str) -> str:
    prefix = config.external_path_prefix.rstrip("/")
    return f"{prefix}{path}" if prefix else path


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8013",
        "http://localhost:8013",
        "http://127.0.0.1:8888",
        "http://localhost:8888",
    ],
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(brands.router)
app.include_router(market_status.router)
app.include_router(cause.router)
app.include_router(deep_analysis.router)
app.include_router(dynamic_market.router)
app.include_router(market_scope.router)
app.include_router(brand_activity.router)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), check_dir=False), name="static")
if config.external_path_prefix:
    app.mount(_prefix_path("/static"), StaticFiles(directory=str(FRONTEND_DIR), check_dir=False), name="prefixed-static")


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse(FRONTEND_FILE)


if config.external_path_prefix:
    app.add_api_route(_prefix_path("/"), serve_frontend, methods=["GET"], include_in_schema=False)
    app.add_api_route(config.external_path_prefix.rstrip("/"), serve_frontend, methods=["GET"], include_in_schema=False)
