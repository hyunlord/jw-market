import json
import os
import sys
import asyncio
import time
import traceback
import shlex
import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.types import Message

import logging
from service import service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================================================================
# [설정값]
# ==================================================================
DEFAULT_MCP_RUN_CMD = (
    "python /opt/jw_mcps/jw_mcps/intended/biomcp-mcp-server/server.py"
)
MCP_RUN_CMD = os.environ.get("MCP_RUN_CMD", DEFAULT_MCP_RUN_CMD)

SERVER_PORT = os.environ.get("SERVER_PORT", "8585")
STREAM_PATH = os.environ.get("STREAM_PATH", "/mcp")
SERVER_HOST = os.environ.get("MCP_INTERNAL_HOST", "127.0.0.1")

MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "3"))
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60"))
IDLE_CHECK_INTERVAL = int(os.environ.get("IDLE_CHECK_INTERVAL", "10"))
STARTUP_TIMEOUT_SECONDS = float(os.environ.get("STARTUP_TIMEOUT_SECONDS", "30"))
STARTUP_POLL_INTERVAL = float(os.environ.get("STARTUP_POLL_INTERVAL", "0.5"))

# hop-by-hop 헤더 — RFC 7230 §6.1
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

logger.info(
    f"[CONFIG] RUN_CMD={MCP_RUN_CMD}, HOST={SERVER_HOST}, PORT={SERVER_PORT}, "
    f"CONCURRENCY={MAX_CONCURRENCY}, IDLE_TIMEOUT={IDLE_TIMEOUT_SECONDS}s, "
    f"IDLE_CHECK={IDLE_CHECK_INTERVAL}s, "
    f"STARTUP_TIMEOUT={STARTUP_TIMEOUT_SECONDS}s"
)

# ==================================================================
# [전역 상태]
# ==================================================================
mcp_process: asyncio.subprocess.Process = None
mcp_lock = asyncio.Lock()
request_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
last_request_time: float = 0.0
active_requests: int = 0
active_lock = asyncio.Lock()
idle_watcher_task: asyncio.Task = None


# ==================================================================
# [Child 생명주기]
# ==================================================================
async def _wait_until_mcp_ready() -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{STREAM_PATH}"
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            if mcp_process is None or mcp_process.returncode is not None:
                logger.error("MCP 프로세스가 죽음 — healthcheck 중단")
                return False
            try:
                res = await client.post(url, json={}, timeout=2.0)
                logger.info(f"✅ MCP healthcheck OK (status={res.status_code})")
                return True
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                pass
            await asyncio.sleep(STARTUP_POLL_INTERVAL)
    logger.error(f"❌ MCP healthcheck timeout ({STARTUP_TIMEOUT_SECONDS}s)")
    return False


async def _start_mcp() -> bool:
    global mcp_process
    try:
        argv = shlex.split(MCP_RUN_CMD)
        logger.info(f"⚡ MCP 기동 (cmd: {MCP_RUN_CMD}, host: {SERVER_HOST}, port: {SERVER_PORT})")
        env = {
            **os.environ,
            "MCP_TRANSPORT": "http",
            "MCP_PORT": SERVER_PORT,
            "MCP_HOSTNAME": SERVER_HOST,
        }
        mcp_process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.info(f"🚀 MCP 시작 (PID: {mcp_process.pid})")
        asyncio.create_task(_stream_mcp_logs(mcp_process.stdout, "stdout"))
        asyncio.create_task(_stream_mcp_logs(mcp_process.stderr, "stderr"))
        if not await _wait_until_mcp_ready():
            logger.error("❌ MCP 부팅 후 healthcheck 실패")
            return False
        return True
    except Exception as e:
        logger.error(f"❌ MCP 실행 실패: {e}")
        mcp_process = None
        return False


async def _stop_mcp():
    global mcp_process
    if mcp_process is None or mcp_process.returncode is not None:
        mcp_process = None
        return
    pid = mcp_process.pid
    try:
        mcp_process.terminate()
        try:
            await asyncio.wait_for(mcp_process.wait(), timeout=5)
            logger.info(f"🛑 MCP 정상 종료 (PID: {pid})")
        except asyncio.TimeoutError:
            mcp_process.kill()
            await mcp_process.wait()
            logger.warning(f"🛑 MCP 강제 종료 (PID: {pid})")
    except Exception as e:
        logger.error(f"MCP 종료 중 오류: {e}")
    finally:
        mcp_process = None


async def _ensure_mcp():
    async with mcp_lock:
        if mcp_process is None or mcp_process.returncode is not None:
            await _start_mcp()


async def _stream_mcp_logs(stream: asyncio.StreamReader, label: str):
    while True:
        try:
            line = await stream.readline()
            if not line:
                break
            logger.info(f"[mcp:{label}] {line.decode().strip()}")
        except Exception:
            break


# ==================================================================
# [Idle 감시]
# ==================================================================
async def _idle_watcher():
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if mcp_process is None or mcp_process.returncode is not None:
                continue
            if last_request_time == 0.0:
                continue
            async with active_lock:
                if active_requests > 0:
                    continue
            idle = time.monotonic() - last_request_time
            if idle >= IDLE_TIMEOUT_SECONDS:
                logger.info(f"💤 {idle:.0f}초 idle (active=0) — MCP 종료")
                async with mcp_lock:
                    await _stop_mcp()
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
        await _stop_mcp()
        logger.info('lifespan end')


app: FastAPI = FastAPI(lifespan=lifespan, title="CDN API (Python)", root_path="/api/cdn")


# ==================================================================
# [미들웨어]
# ==================================================================
def log_request(request, req_body, resp_body):
    if 'upload' in request.url.path:
        req_body = "[FILE UPLOAD]"
    else:
        try:
            req_body = req_body.decode("utf-8")
        except Exception:
            req_body = "[binary]"
    try:
        resp_body_str = resp_body.decode("utf-8")[:1000]
    except Exception:
        resp_body_str = "[binary]"
    logger.info(f'req: ip={request.client.host} method={request.method} path={request.url.path} params={request.query_params} req_body={req_body}')
    logger.info(f'resp: {resp_body_str}')


@app.middleware("http")
async def http_middleware(request, call_next):
    req_body = await request.body()
    response = await call_next(request)
    # StreamingResponse는 미들웨어에서 본문 읽으면 깨지므로 그대로 통과
    if isinstance(response, StreamingResponse):
        logger.info(f'req(stream): ip={request.client.host} method={request.method} path={request.url.path}')
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
        "run_cmd": MCP_RUN_CMD,
        "port": SERVER_PORT,
    }


def _filter_request_headers(headers) -> dict:
    """클라이언트 → MCP 요청 헤더 정제."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
        and k.lower() != "host"
    }


def _filter_response_headers(headers) -> dict:
    """MCP → 클라이언트 응답 헤더 정제 (mcp-session-id 등 모두 보존)."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


@app.get(path="/json")
@app.get(path="/json/sse")
async def relay_get(request: Request):
    """GET 릴레이 — SSE 구독용."""
    global last_request_time
    last_request_time = time.monotonic()
    await _ensure_mcp()

    client = httpx.AsyncClient(timeout=httpx.Timeout(None))
    proxy_headers = _filter_request_headers(request.headers)

    req = client.build_request(
        "GET",
        url=f"http://{SERVER_HOST}:{SERVER_PORT}{STREAM_PATH}",
        headers=proxy_headers,
        params=dict(request.query_params),
    )
    res = await client.send(req, stream=True)
    forward_headers = _filter_response_headers(res.headers)
    upstream_content_type = res.headers.get("content-type", "")
    if upstream_content_type.lower().startswith("text/event-stream") and "charset=" not in upstream_content_type.lower():
        forward_headers["content-type"] = "text/event-stream; charset=utf-8"

    async def sse_body():
        try:
            async for chunk in res.aiter_raw():
                yield chunk
        finally:
            await res.aclose()
            await client.aclose()

    return StreamingResponse(
        sse_body(),
        status_code=res.status_code,
        headers=forward_headers,
        media_type=res.headers.get("content-type", "text/event-stream"),
    )


@app.post(path="")
@app.post(path="/json")
async def relay(request: Request, body: dict):
    """MCP 요청을 내부 FastMCP HTTP 서버로 중계 (스트리밍)."""
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
                await _ensure_mcp()

                # 스트리밍 클라이언트 (응답 끝날 때까지 살아있어야 함)
                client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None))
                proxy_headers = _filter_request_headers(request.headers)

                req = client.build_request(
                    "POST",
                    url=f"http://{SERVER_HOST}:{SERVER_PORT}{STREAM_PATH}",
                    json=data,
                    headers=proxy_headers,
                )
                res = await client.send(req, stream=True)
                last_request_time = time.monotonic()

                forward_headers = _filter_response_headers(res.headers)

                upstream_content_type = res.headers.get("content-type", "")

                if upstream_content_type.lower().startswith("text/event-stream") and "charset=" not in upstream_content_type.lower():

                    forward_headers["content-type"] = "text/event-stream; charset=utf-8"

                async def body_iterator():
                    global active_requests
                    try:
                        async for chunk in res.aiter_raw():
                            yield chunk
                    finally:
                        await res.aclose()
                        await client.aclose()
                        async with active_lock:
                            active_requests -= 1

                return StreamingResponse(
                    body_iterator(),
                    status_code=res.status_code,
                    headers=forward_headers,
                    media_type=res.headers.get("content-type"),
                )
            except Exception:
                # try 블록 중 예외 시 카운터 보정
                async with active_lock:
                    active_requests -= 1
                raise

    last_request_time = time.monotonic()
    await _ensure_mcp()
    return {
        "status": "running",
        "run_cmd": MCP_RUN_CMD,
        "port": SERVER_PORT,
    }


@app.post(path="/multipart")
async def multipart_run(request: Request):
    return {"status": "multipart received"}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8086, reload=True)
