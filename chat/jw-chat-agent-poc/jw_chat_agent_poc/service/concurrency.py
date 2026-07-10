from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

BUSY_MESSAGE = "현재 사용자가 많습니다. 잠시 후 다시 시도해주세요."

CHAT_MAX_CONCURRENCY_ENV = "CHAT_MAX_CONCURRENCY"
CHAT_QUEUE_WAIT_S_ENV = "CHAT_QUEUE_WAIT_S"

DEFAULT_MAX_CONCURRENCY = 3
DEFAULT_QUEUE_WAIT_S = 10.0


class ChatBusyError(RuntimeError):
    def __init__(self, message: str = BUSY_MESSAGE) -> None:
        super().__init__(message)


class ChatConcurrencyLimiter:
    """Caps in-flight chat answering so rejected requests never reach the LLM."""

    def __init__(self, max_concurrency: int | None = None, queue_wait_s: float | None = None) -> None:
        if max_concurrency is None:
            max_concurrency = int(os.environ.get(CHAT_MAX_CONCURRENCY_ENV, str(DEFAULT_MAX_CONCURRENCY)))
        if queue_wait_s is None:
            queue_wait_s = float(os.environ.get(CHAT_QUEUE_WAIT_S_ENV, str(DEFAULT_QUEUE_WAIT_S)))
        self.max_concurrency = max(1, max_concurrency)
        self.queue_wait_s = max(0.0, queue_wait_s)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    def try_acquire(self) -> bool:
        return self._semaphore.acquire(timeout=self.queue_wait_s)

    def release(self) -> None:
        self._semaphore.release()

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self.try_acquire():
            raise ChatBusyError()
        try:
            yield
        finally:
            self.release()
