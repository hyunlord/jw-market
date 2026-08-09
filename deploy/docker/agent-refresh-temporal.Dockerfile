FROM python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553

ARG APP_VERSION="local"
ARG SOURCE_TREE="unknown"
ARG BUILD_PURPOSE="agent2-agent3-weekly-temporal"
ARG KUBECTL_VERSION="v1.30.0"
ARG KUBECTL_SHA256="7c3807c0f5c1b30110a2ff1e55da1d112a6d0096201f1beb81b269f582b5d1c5"

LABEL org.opencontainers.image.revision="${APP_VERSION}" \
      org.opencontainers.image.source-tree="${SOURCE_TREE}" \
      build-purpose="${BUILD_PURPOSE}"

ENV APP_VERSION="${APP_VERSION}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd -g 3000 app \
    && useradd -u 3000 -g 3000 -m -s /bin/bash app \
    && pip install --no-cache-dir "temporalio==1.30.0" \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl', '/usr/local/bin/kubectl')" \
    && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl \
    && /usr/local/bin/kubectl version --client=true

WORKDIR /app
COPY pipeline/__init__.py /app/pipeline/__init__.py
COPY pipeline/scripts/agent_refresh_weekly /app/pipeline/scripts/agent_refresh_weekly

USER app
CMD ["python", "-m", "pipeline.scripts.agent_refresh_weekly.temporal_worker"]
