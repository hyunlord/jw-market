import json
import os
import sys
import asyncio
import time
import traceback
import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.types import Message

from common.logger import Logger
from service import service

logger = Logger.getLogger(__name__)

# ==================================================================
# [설정값] — 환경변수에서 읽고, 없으면 기본값
# Pod별로 다른 env를 주입해서 MCP를 분리
# ==================================================================
MCP_BUNDLE_PATH = os.environ.get(
    "MCP_BUNDLE_PATH",
    "/opt/jw_mcps/jw_mcps/bmt_mcps/OpenTargets-MCP-Server/dist/package/bundle.js"
)

SERVER_PORT = os.environ.get("SERVER_PORT", "8585")
STREAM_PATH = os.environ.get("STREAM_PATH", "/mcp")

MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "3"))
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60"))
IDLE_CHECK_INTERVAL = int(os.environ.get("IDLE_CHECK_INTERVAL", "10"))

logger.info(
    f"[CONFIG] BUNDLE={MCP_BUNDLE_PATH}, PORT={SERVER_PORT}, "
    f"CONCURRENCY={MAX_CONCURRENCY}, IDLE_TIMEOUT={IDLE_TIMEOUT_SECONDS}s, "
    f"IDLE_CHECK={IDLE_CHECK_INTERVAL}s"
)

# ==================================================================
# [전역 상태]
# ==================================================================
gateway_process: asyncio.subprocess.Process = None
gateway_lock = asyncio.Lock()
request_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
last_request_time: float = 0.0
active_requests: int = 0
active_lock = asyncio.Lock()
idle_watcher_task: asyncio.Task = None


# ==================================================================
# [Child 생명주기]
# ==================================================================
async def _start_supergateway() -> bool:
    """supergateway 프로세스 부팅."""
    global gateway_process

    if not os.path.exists(MCP_BUNDLE_PATH):
        logger.error(f"❌ bundle.js 없음: {MCP_BUNDLE_PATH}")
        return False

    try:
        logger.info(f"⚡ supergateway 기동 (bundle: {MCP_BUNDLE_PATH}, port: {SERVER_PORT})")
        cmd_args = [
            "--host", "127.0.0.1",
            "--stdio", f"node {MCP_BUNDLE_PATH}",
            "--port", SERVER_PORT,
            "--outputTransport", "streamableHttp",
            "--streamableHttpPath", STREAM_PATH
        ]
        gateway_process = await asyncio.create_subprocess_exec(
            "supergateway", *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        logger.info(f"🚀 supergateway 시작 (PID: {gateway_process.pid})")

        asyncio.create_task(_stream_gateway_logs(gateway_process.stdout))
        asyncio.create_task(_stream_gateway_logs(gateway_process.stderr))

        await asyncio.sleep(2)
        return True
    except Exception as e:
        logger.error(f"❌ supergateway 실행 실패: {e}")
        gateway_process = None
        return False


async def _stop_supergateway():
    """안전 종료 (SIGTERM → 5초 대기 → SIGKILL)."""
    global gateway_process

    if gateway_process is None or gateway_process.returncode is not None:
        gateway_process = None
        return

    pid = gateway_process.pid
    try:
        gateway_process.terminate()
        try:
            await asyncio.wait_for(gateway_process.wait(), timeout=5)
            logger.info(f"🛑 supergateway 정상 종료 (PID: {pid})")
        except asyncio.TimeoutError:
            gateway_process.kill()
            await gateway_process.wait()
            logger.warning(f"🛑 supergateway 강제 종료 (PID: {pid})")
    except Exception as e:
        logger.error(f"supergateway 종료 중 오류: {e}")
    finally:
        gateway_process = None


async def _ensure_supergateway():
    """child가 죽어있으면 띄움. lock으로 동시 부팅 방지."""
    async with gateway_lock:
        if gateway_process is None or gateway_process.returncode is not None:
            await _start_supergateway()


async def _stream_gateway_logs(stream: asyncio.StreamReader):
    while True:
        try:
            line = await stream.readline()
            if not line:
                break
            logger.info(f"[supergateway] {line.decode().strip()}")
        except Exception:
            break


# ==================================================================
# [Idle 감시] — 처리 중인 요청 있으면 안 죽임
# ==================================================================
async def _idle_watcher():
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if gateway_process is None or gateway_process.returncode is not None:
                continue
            if last_request_time == 0.0:
                continue

            async with active_lock:
                if active_requests > 0:
                    continue

            idle = time.monotonic() - last_request_time
            if idle >= IDLE_TIMEOUT_SECONDS:
                logger.info(f"💤 {idle:.0f}초 idle (active=0) — supergateway 종료")
                async with gateway_lock:
                    await _stop_supergateway()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"idle watcher 오류: {e}")


# ==================================================================
# [Lifespan]
# ==================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global idle_watcher_task
    logger.info('lifespan start')
    idle_watcher_task = asyncio.create_task(_idle_watcher())
    try:
        yield
    finally:
        logger.info('lifespan shutdown — child 정리')
        if idle_watcher_task is not None:
            idle_watcher_task.cancel()
            try:
                await idle_watcher_task
            except asyncio.CancelledError:
                pass
        await _stop_supergateway()
        logger.info('lifespan end')


app: FastAPI = FastAPI(lifespan=lifespan, title="CDN API", root_path="/api/cdn")


# ==================================================================
# [미들웨어]
# ==================================================================
def log_request(request, req_body, resp_body):
    if 'upload' in request.url.path:
        req_body = "[FILE UPLOAD]"
    else:
        req_body = req_body.decode("utf-8")
    resp_body = resp_body.decode("utf-8")
    logger.info(f'req: ip={request.client.host} method={request.method} path={request.url.path} params={request.query_params} req_body={req_body}')
    logger.info(f'resp: {resp_body}')


@app.middleware("http")
async def http_middleware(request, call_next):
    req_body = await request.body()
    response = await call_next(request)
    if isinstance(response, StreamingResponse):
        return response
    res_body = b''
    async for chunk in response.body_iterator:
        res_body += chunk
    task = BackgroundTask(log_request, request, req_body, res_body)
    return Response(
        content=res_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
        background=task
    )


# ==================================================================
# [엔드포인트]
# ==================================================================
@app.get(path="")
@app.get(path="/health")
async def health(request: Request):
    return {
        "status": "ok",
        "bundle": MCP_BUNDLE_PATH,
        "port": SERVER_PORT,
    }


@app.post(path="")
@app.post(path="/json")
async def relay(request: Request, body: dict):
    """MCP 요청을 supergateway로 중계."""
    global last_request_time, active_requests

    try:
        data = await request.json()
    except json.decoder.JSONDecodeError:
        data = await request.body()

    if isinstance(data, dict) and ("jsonrpc" in data or "method" in data):
        async with request_semaphore:
            async with active_lock:
                active_requests += 1
            try:
                last_request_time = time.monotonic()
                await _ensure_supergateway()

                proxy_headers = {
                    k: v for k, v in request.headers.items()
                    if k.lower() != "content-length"
                }

                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        url=f"http://127.0.0.1:{SERVER_PORT}{STREAM_PATH}",
                        json=data,
                        headers=proxy_headers,
                        timeout=60.0
                    )
                    last_request_time = time.monotonic()
                    response_headers = dict(res.headers)
                    upstream_content_type = response_headers.get("content-type", "")
                    if upstream_content_type.lower().startswith("text/event-stream") and "charset=" not in upstream_content_type.lower():
                        response_headers["content-type"] = "text/event-stream; charset=utf-8"
                    return Response(
                        content=res.content,
                        status_code=res.status_code,
                        headers=response_headers
                    )
            finally:
                async with active_lock:
                    active_requests -= 1

    last_request_time = time.monotonic()
    await _ensure_supergateway()
    return {
        "status": "running",
        "bundle": MCP_BUNDLE_PATH,
        "port": SERVER_PORT,
    }


@app.post(path="/multipart")
async def multipart_run(request: Request):
    return {"status": "multipart received"}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8086, reload=True)
