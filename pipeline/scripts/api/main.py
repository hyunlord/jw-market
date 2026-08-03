from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.actor_assertion import (
    ActorAssertionConfig,
    install_actor_assertion_middleware,
)
from pipeline.scripts.api.audit_logging import (
    AsyncAuditWriter,
    create_audit_writer,
    install_audit_logging_middleware,
)
from pipeline.scripts.api.config import config
from pipeline.scripts.api.db import close_pool, init_pool
from pipeline.scripts.api.openapi_docs import install_openapi_overrides
from pipeline.scripts.api.report_download_logging import (
    AsyncReportDownloadWriter,
    create_report_download_router,
    create_report_download_writer,
)
from pipeline.scripts.api.dashboard_usage import (
    DashboardCache,
    MariaDBUsageRepository,
    UsageStatsService,
)
from pipeline.scripts.api.routes.dashboard_usage import (
    create_usage_dashboard_router,
    create_usage_logs_router,
)
from pipeline.scripts.api.routes import (
    brand_activity,
    brands,
    capabilities,
    cause,
    deep_analysis,
    dynamic_market,
    health,
    market_filter,
    market_scope,
    market_status,
)

logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)
audit_writer = create_audit_writer(config)
report_download_writer = create_report_download_writer(config)
usage_dashboard_repository = MariaDBUsageRepository(config) if config.dashboard_db_host else None
usage_dashboard_service = (
    UsageStatsService(usage_dashboard_repository, cache=DashboardCache(ttl_seconds=60))
    if usage_dashboard_repository is not None
    else None
)


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
    if isinstance(audit_writer, AsyncAuditWriter):
        audit_writer.start()
    if isinstance(report_download_writer, AsyncReportDownloadWriter):
        report_download_writer.start()
    logger.info(
        "JW Market API starting: version=%s prefix=%s db=%s:%s/%s",
        config.app_version,
        config.external_path_prefix,
        config.db_host,
        config.db_port,
        config.db_name,
    )
    yield
    if isinstance(audit_writer, AsyncAuditWriter):
        audit_writer.stop()
    if isinstance(report_download_writer, AsyncReportDownloadWriter):
        report_download_writer.stop()
    close_pool()


app = FastAPI(
    title="JW Market Analysis API",
    version=config.app_version,
    root_path=config.external_path_prefix,
    lifespan=lifespan,
)

install_actor_assertion_middleware(app, ActorAssertionConfig.from_api_config(config))
install_audit_logging_middleware(app, audit_writer)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8013",
        "http://localhost:8013",
        "http://127.0.0.1:8888",
        "http://localhost:8888",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    compresslevel=1,
)

app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(brands.router)
app.include_router(market_status.router)
app.include_router(cause.router)
app.include_router(deep_analysis.router)
app.include_router(dynamic_market.router)
app.include_router(market_filter.router)
app.include_router(market_scope.router)
app.include_router(brand_activity.router)
app.include_router(create_report_download_router(report_download_writer))
if usage_dashboard_service is not None and usage_dashboard_repository is not None:
    app.include_router(create_usage_dashboard_router(usage_dashboard_service))
    app.include_router(create_usage_logs_router(usage_dashboard_repository))

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), check_dir=False), name="static")
if config.external_path_prefix:
    app.mount(_prefix_path("/static"), StaticFiles(directory=str(FRONTEND_DIR), check_dir=False), name="prefixed-static")


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse(FRONTEND_FILE)


if config.external_path_prefix:
    app.add_api_route(_prefix_path("/"), serve_frontend, methods=["GET"], include_in_schema=False)
    app.add_api_route(config.external_path_prefix.rstrip("/"), serve_frontend, methods=["GET"], include_in_schema=False)

install_openapi_overrides(app)
