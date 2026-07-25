from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass


@dataclass(slots=True)
class HiraHttpClient:
    request_delay_seconds: float = 0.50
    timeout_seconds: float = 20.0
    user_agent: str = "JW-Market-HIRA-Benefit/1.0 (contact: data-ops)"
    _last_request_at: float | None = None

    def get_text(self, url: str) -> str:
        if not url.startswith("https://www.hira.or.kr/"):
            raise ValueError("HIRA client refuses non-approved origins")
        if self._last_request_at is not None:
            remaining = self.request_delay_seconds - (
                time.monotonic() - self._last_request_at
            )
            if remaining > 0:
                time.sleep(remaining)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_charset() or "utf-8"
            payload = response.read()
        self._last_request_at = time.monotonic()
        return payload.decode(content_type, errors="replace")
