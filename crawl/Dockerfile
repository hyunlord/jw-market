# 2-tier crawl runtime image.
#
# CronJobs override the command and call scripts under crawl/ directly, so this
# image keeps /app as the repository root and copies the crawl tree unchanged.

FROM python:3.11-slim

ARG APP_VERSION="local"

ENV APP_VERSION="${APP_VERSION}" \
    PROJECT_ROOT="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app:/app/crawl/crawler:/app/crawl/agent1"

RUN groupadd -g 3000 app \
    && useradd -u 3000 -g 3000 -m -s /bin/bash app

WORKDIR /app

RUN pip install --no-cache-dir \
    PyMySQL \
    beautifulsoup4 \
    python-dotenv \
    requests \
    trafilatura

COPY crawl /app/crawl

RUN chown -R app:app /app

USER app

CMD ["python", "--version"]
