FROM python:3.11-slim

ARG APP_VERSION="local"
ARG SOURCE_TREE="unknown"
ARG BUILD_PURPOSE="agent2-agent3-weekly-temporal"

LABEL org.opencontainers.image.revision="${APP_VERSION}" \
      org.opencontainers.image.source-tree="${SOURCE_TREE}" \
      build-purpose="${BUILD_PURPOSE}"

ENV APP_VERSION="${APP_VERSION}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd -g 3000 app \
    && useradd -u 3000 -g 3000 -m -s /bin/bash app \
    && pip install --no-cache-dir "temporalio==1.30.0" \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl', '/usr/local/bin/kubectl')" \
    && chmod +x /usr/local/bin/kubectl \
    && /usr/local/bin/kubectl version --client=true

WORKDIR /app
COPY pipeline/__init__.py /app/pipeline/__init__.py
COPY pipeline/scripts/agent_refresh_weekly /app/pipeline/scripts/agent_refresh_weekly

USER app
CMD ["python", "-m", "pipeline.scripts.agent_refresh_weekly.temporal_worker"]
