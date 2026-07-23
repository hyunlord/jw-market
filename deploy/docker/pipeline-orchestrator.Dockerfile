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
    && pip install --no-cache-dir "openpyxl>=3.1" "typer>=0.12" "requests>=2.31" \
    && pip install --no-cache-dir "pyarrow==24.0.0" "duckdb==1.5.4"
# pyarrow/duckdb belong to the ETL load path (`python -m pipeline.etl.run`), not
# the api backend, so they live here and NOT in the shared requirements.txt:
#   pyarrow  <- s1_load -> io/iqvia_loader.py (parquet source + catalog R/W)
#   duckdb   <- s3_enrich -> io/enrich/iqvia_nsa_bridge.py
# Both are import-time (module-level) on `pipeline.etl.run`; without them the
# real incremental load aborts before arg-parse. Versions pinned for
# reproducibility and chosen for pandas 3.0 / numpy 2.4 compatibility (cp311
# wheels, no build step). prophet is deliberately absent (contract environment;
# forecast runs on statsmodels — adding prophet would change the computation).

# kubectl for the event-driven wake-ups (ETL kick / CSD sensor create Jobs
# via the jw-pipeline-kicker ServiceAccount). Fetched with python urllib
# because the build host has PyPI/HTTPS egress but no apt mirror access.
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl', '/usr/local/bin/kubectl')" \
    && chmod +x /usr/local/bin/kubectl \
    && /usr/local/bin/kubectl version --client=true

COPY pipeline /app/pipeline
COPY deploy/k8s/orchestrator/pipeline-orchestrator-full-rehearsal-job.yaml /app/deploy/k8s/orchestrator/pipeline-orchestrator-full-rehearsal-job.yaml
COPY docs/crawl /app/docs/crawl
COPY ["data/JW 주요 약품 수동 매핑", "/app/data/JW 주요 약품 수동 매핑"]
# Git-tracked s2 catalog seeds (target_priority skeleton + molecule worklist). The
# R-1 rehearse-full catalog step reads them via --cache-dir /app/data/cache and
# --inputs-dir /app/inputs; without them a fresh isolated rebuild aborts in
# run_target_priority / catalog_postfix. Explicit single-file COPYs (not the whole
# data/cache or inputs dir) so untracked local artifacts never enter the image.
COPY data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv /app/data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv
COPY inputs/molecule_v4_worklist.csv /app/inputs/molecule_v4_worklist.csv

RUN mkdir -p /var/lib/jw-pipeline && chown -R app:app /app /var/lib/jw-pipeline

USER app

CMD ["python", "-m", "pipeline.orchestrator", "run", "--dry-run"]
