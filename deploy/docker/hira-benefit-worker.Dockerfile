FROM python:3.11-slim

ARG APP_VERSION="local"

ENV APP_VERSION="${APP_VERSION}" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/work

RUN pip install --no-cache-dir \
    "anyio==4.10.0" \
    "PyMySQL==1.1.1" \
    "temporalio==1.20.0"

WORKDIR /work

COPY pipeline/ /work/pipeline/

CMD ["python", "-m", "pipeline.scripts.crawler.hira_benefit.temporal_worker"]
