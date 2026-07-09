# JW Market backend API image.
#
# This Dockerfile restores the historical API image shape used by the
# running backend: Python 3.11, /app workdir, non-root UID/GID 3000, and a
# uvicorn entrypoint for pipeline.scripts.api.main:app.  The current scoped
# backend imports pipeline.etl helpers from the dynamic-market resolver, so
# pipeline/etl is included as runtime support.

FROM python:3.11-slim

ARG APP_VERSION="local"

ENV APP_VERSION="${APP_VERSION}" \
    PYTHONUNBUFFERED=1 \
    API_HOST="0.0.0.0" \
    API_PORT="8000" \
    LOG_LEVEL="INFO"

RUN groupadd -g 3000 app \
    && useradd -u 3000 -g 3000 -m -s /bin/bash app

WORKDIR /app

COPY pipeline/scripts/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY pipeline/scripts/api /app/pipeline/scripts/api
COPY pipeline/scripts/analysis /app/pipeline/scripts/analysis
COPY pipeline/scripts/etl /app/pipeline/scripts/etl
COPY pipeline/scripts/forecast /app/pipeline/scripts/forecast
COPY pipeline/scripts/utils /app/pipeline/scripts/utils
COPY pipeline/etl /app/pipeline/etl
COPY docs/reference /app/static

RUN chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:%s%s/api/health' % (os.getenv('API_PORT', '8000'), os.getenv('EXTERNAL_PATH_PREFIX', '')), timeout=5).read()" || exit 1

CMD ["sh", "-c", "exec uvicorn pipeline.scripts.api.main:app --host \"$API_HOST\" --port \"$API_PORT\" --workers 1 --log-level \"$(printf '%s' \"$LOG_LEVEL\" | tr '[:upper:]' '[:lower:]')\""]
