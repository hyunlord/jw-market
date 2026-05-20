from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import close_pool, init_pool  # noqa: E402
from pipeline.scripts.api.routes import brands, cause, deep_analysis, health, market_status  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


app = FastAPI(
    title="JW Market Analysis API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(brands.router)
app.include_router(market_status.router)
app.include_router(cause.router)
app.include_router(deep_analysis.router)
