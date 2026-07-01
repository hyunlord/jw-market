# wf301 VDB registration bridge — dry-run skeleton
# 규약(조사 확정): workingDir /app, port 8080, command supervisord -n, FastAPI+gunicorn+uvicorn
FROM python:3.12-slim

# code-serving 표준 계정: 기존 template-code-serving과 같은 uid/gid.
ARG UID=3000
ARG GID=3000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid "${GID}" genos \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin genos

# 의존성 먼저 (레이어 캐시)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 앱 + 설정
COPY --chown=genos:genos src /app/src
COPY --chown=genos:genos gunicorn_conf.py /app/gunicorn_conf.py
COPY supervisor/supervisord.conf /etc/supervisor/supervisord.conf

RUN chown -R genos:genos /app /tmp

EXPOSE 8080

# health check (code-serving startupProbe가 /health를 GET)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8080/health',timeout=3).status_code==200 else 1)"

# code-serving template 규약: supervisord -n
USER 3000:3000
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
