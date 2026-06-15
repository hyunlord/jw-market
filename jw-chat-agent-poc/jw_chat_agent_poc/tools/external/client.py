from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import time
from typing import Any
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import requests


DATA_GO_KR_KEY_ENV = "DATA_GO_KR_KEY"


@dataclass(frozen=True)
class ExternalCall:
    tool: str
    source: str
    status: str
    summary_text: str
    render_data: dict[str, Any]
    safe_url: str | None = None
    elapsed_ms: float | None = None


class ExternalApiClient:
    def __init__(self, mode: str = "fixture", fixture_path: Path | None = None, timeout_s: int = 12) -> None:
        self.mode = mode
        self.timeout_s = timeout_s
        path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "external_api_fixtures.json"
        self.fixtures = json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def redact_url(url: str) -> str:
        if "serviceKey=" not in url:
            return url
        prefix, rest = url.split("serviceKey=", 1)
        if "&" in rest:
            return f"{prefix}serviceKey=<redacted>&{rest.split('&', 1)[1]}"
        return f"{prefix}serviceKey=<redacted>"

    def mfds_permission_search(self, brand: str) -> ExternalCall:
        return self._fixture_or_live("mfds_permission_search", {"brand": brand})

    def mfds_permission_detail(self, item_seq: str) -> ExternalCall:
        return self._fixture_or_live("mfds_permission_detail", {"item_seq": item_seq})

    def mfds_composition(self, item_seq: str) -> ExternalCall:
        return self._fixture_or_live("mfds_composition", {"item_seq": item_seq})

    def mfds_easy_drug(self, item_seq: str) -> ExternalCall:
        return self._fixture_or_live("mfds_easy_drug", {"item_seq": item_seq})

    def mfds_clinical_trial_kr(self, keyword: str) -> ExternalCall:
        return self._fixture_or_live("mfds_clinical_trial_kr", {"keyword": keyword})

    def clinicaltrials_v2_search(self, query_intr: str) -> ExternalCall:
        return self._fixture_or_live("clinicaltrials_v2_search", {"query.intr": query_intr})

    def openfda_label_search(self, substance_name: str) -> ExternalCall:
        return self._fixture_or_live("openfda_label_search", {"substance_name": substance_name})

    def mfds_patent(self, ingredient_en: str) -> ExternalCall:
        return self._fixture_or_live("mfds_patent", {"INGR_ENG_NAME": ingredient_en}, xml=True)

    def mfds_fda_orangebook(self, ingredient_en: str) -> ExternalCall:
        return self._fixture_or_live("mfds_fda_orangebook", {"INGR_NAME": ingredient_en}, xml=True)

    def _fixture_or_live(self, tool: str, params: dict[str, str], xml: bool = False) -> ExternalCall:
        if self.mode != "live":
            data = self.fixtures[tool]
            return ExternalCall(
                tool=tool,
                source="external_api",
                status="fixture",
                summary_text=data["summary_text"],
                render_data={**data["render_data"], "request": params},
                safe_url=data.get("safe_url"),
                elapsed_ms=0.0,
            )
        return self._live_get(tool, params, xml=xml)

    def _live_get(self, tool: str, params: dict[str, str], xml: bool = False) -> ExternalCall:
        spec = self.fixtures[tool]["live"]
        query = dict(params)
        if spec.get("requires_service_key"):
            key = os.environ.get(DATA_GO_KR_KEY_ENV)
            if not key:
                raise RuntimeError(f"{DATA_GO_KR_KEY_ENV} is required for live {tool}")
            query["serviceKey"] = key
        url = spec["url"] + "?" + urlencode(query)
        start = time.monotonic()
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = requests.get(url, timeout=self.timeout_s)
                elapsed = round((time.monotonic() - start) * 1000, 1)
                response.raise_for_status()
                payload = self._parse_xml(response.text) if xml else response.json()
                return ExternalCall(
                    tool=tool,
                    source="external_api",
                    status="live",
                    summary_text=f"{tool} returned HTTP {response.status_code}",
                    render_data={"payload": payload},
                    safe_url=self.redact_url(url),
                    elapsed_ms=elapsed,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        elapsed = round((time.monotonic() - start) * 1000, 1)
        return ExternalCall(
            tool=tool,
            source="external_api",
            status="error",
            summary_text=f"{tool} failed: {last_error}",
            render_data={"error": str(last_error) if last_error else "unknown"},
            safe_url=self.redact_url(url),
            elapsed_ms=elapsed,
        )

    @staticmethod
    def _parse_xml(text: str) -> dict[str, Any]:
        root = ET.fromstring(text)
        out: dict[str, Any] = {}
        for child in root.iter():
            if child is root or list(child):
                continue
            out.setdefault(child.tag, []).append(child.text)
        return out
