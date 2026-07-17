# JW Market pipeline orchestrator image.
#
# Runs `python -m pipeline.orchestrator run` (the monthly chain single entry
# point) plus every canonical builder it shells out to. Unlike the backend
# api/Dockerfile (protected contract blob, explicit COPY list), this image
# carries the whole pipeline package so the orchestrator, builders, and
# standalone scripts share one filesystem layout identical to the repo.
#
# Build (from the repo root, linux/amd64):
#   docker build --platform linux/amd64 -f deploy/docker/pipeline-orchestrator.Dockerfile \
#     --build-arg APP_VERSION=<tag> -t <registry>/jw-pipeline-orchestrator:<tag> .
#
# There is deliberately NO baked AGENT3_WORKFLOW_REV: the strength stage fails
# closed unless the CronJob manifest pins the revision env.

FROM python:3.11-slim

ARG APP_VERSION="local"

ENV APP_VERSION="${APP_VERSION}" \
    PROJECT_ROOT="/app" \
    PYTHONUNBUFFERED=1

RUN groupadd -g 3000 app \
    && useradd -u 3000 -g 3000 -m -s /bin/bash app

WORKDIR /app

COPY pipeline/scripts/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir "openpyxl>=3.1" "typer>=0.12" "requests>=2.31"

# kubectl for the event-driven wake-ups (ETL kick / CSD sensor create Jobs
# via the jw-pipeline-kicker ServiceAccount).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY pipeline /app/pipeline
COPY docs/crawl /app/docs/crawl

RUN mkdir -p /var/lib/jw-pipeline && chown -R app:app /app /var/lib/jw-pipeline

USER app

CMD ["python", "-m", "pipeline.orchestrator", "run", "--dry-run"]
