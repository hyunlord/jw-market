# gunicorn 설정 — code-serving 규약: 0.0.0.0:8080, uvicorn worker.
import os

bind = "0.0.0.0:8080"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
worker_class = "uvicorn.workers.UvicornWorker"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 20
keepalive = 5
# 로그는 stdout/stderr (supervisor가 수집)
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
