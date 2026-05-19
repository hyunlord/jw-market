FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends     git     && rm -rf /var/lib/apt/lists/*

COPY pipeline/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY pipeline /app/pipeline
COPY catalog /app/catalog

COPY pipeline/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

VOLUME ["/data", "/output"]

ENV PROJECT_ROOT=/app
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

ENTRYPOINT ["/app/entrypoint.sh"]
