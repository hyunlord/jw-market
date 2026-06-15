from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import xml.etree.ElementTree as ET

import requests


DATA_GO_KR_KEY_ENV = "DATA_GO_KR_KEY"
MFDS_PATENT_QUERY_ALIASES = {
    "pitavastatin": "리바로",
    "ezetimibe": "리바로젯",
}


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
        query = f'openfda.substance_name:"{substance_name.upper()}"'
        return self._fixture_or_live("openfda_label_search", {"search": query})

    def mfds_patent(self, ingredient_en: str) -> ExternalCall:
        query = MFDS_PATENT_QUERY_ALIASES.get(ingredient_en.lower(), ingredient_en)
        return self._fixture_or_live("mfds_patent", {"query": query}, xml=True)

    def mfds_fda_orangebook(self, ingredient_en: str) -> ExternalCall:
        return self._fixture_or_live("mfds_fda_orangebook", {"query": ingredient_en.title()}, xml=True)

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
        query = self._live_query(spec, params)
        if spec.get("requires_service_key"):
            key = os.environ.get(DATA_GO_KR_KEY_ENV)
            if not key:
                raise RuntimeError(f"{DATA_GO_KR_KEY_ENV} is required for live {tool}")
            query["serviceKey"] = key
        url = self._url_with_query(spec["url"], query)
        start = time.monotonic()
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = requests.get(url, timeout=self.timeout_s)
                elapsed = round((time.monotonic() - start) * 1000, 1)
                response.raise_for_status()
                payload = self._parse_response(response, xml or spec.get("format") == "xml")
                return ExternalCall(
                    tool=tool,
                    source="external_api",
                    status="live",
                    summary_text=self._summary(tool, response.status_code, payload),
                    render_data=self._render_payload(payload),
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
    def _live_query(spec: dict[str, Any], params: dict[str, str]) -> dict[str, str]:
        mapped: dict[str, str] = {}
        for key, value in spec.get("default_params", {}).items():
            mapped[key] = str(value)
        param_map = spec.get("param_map", {})
        for key, value in params.items():
            target = param_map.get(key, key)
            if target:
                mapped[target] = value
        return mapped

    @staticmethod
    def _url_with_query(url: str, query: dict[str, str]) -> str:
        parts = urlsplit(url)
        existing = dict(parse_qsl(parts.query, keep_blank_values=True))
        existing.update(query)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query and urlencode(existing) or urlencode(existing), parts.fragment))

    @staticmethod
    def _parse_response(response: requests.Response, xml: bool) -> dict[str, Any]:
        if xml:
            return ExternalApiClient._parse_xml(response.text)
        return response.json()

    @staticmethod
    def _render_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if "body" in payload and isinstance(payload["body"], dict):
            body = payload["body"]
            items = body.get("items", [])
            if isinstance(items, list):
                return {
                    "resultCode": payload.get("header", {}).get("resultCode"),
                    "totalCount": body.get("totalCount"),
                    "items": items[:5],
                }
        if "studies" in payload and isinstance(payload["studies"], list):
            return {"payload": {"studies": payload["studies"][:5], "nextPageToken": payload.get("nextPageToken")}}
        if "results" in payload and isinstance(payload["results"], list):
            meta = payload.get("meta", {})
            return {"payload": {"meta": meta, "results": payload["results"][:1]}}
        return {"payload": payload}

    @staticmethod
    def _summary(tool: str, status_code: int, payload: dict[str, Any]) -> str:
        if "body" in payload and isinstance(payload["body"], dict):
            total = payload["body"].get("totalCount")
            return f"{tool} returned HTTP {status_code}, totalCount={total}"
        if "studies" in payload and isinstance(payload["studies"], list):
            ids = []
            for study in payload["studies"][:3]:
                ident = study.get("protocolSection", {}).get("identificationModule", {})
                if ident.get("nctId"):
                    ids.append(ident["nctId"])
            return f"{tool} returned HTTP {status_code}, nct_ids={','.join(ids)}"
        if "results" in payload and isinstance(payload["results"], list):
            total = payload.get("meta", {}).get("results", {}).get("total")
            return f"{tool} returned HTTP {status_code}, total={total}"
        return f"{tool} returned HTTP {status_code}"

    @staticmethod
    def _parse_xml(text: str) -> dict[str, Any]:
        root = ET.fromstring(text)
        return ExternalApiClient._xml_node(root)

    @staticmethod
    def _xml_node(node: ET.Element) -> Any:
        children = list(node)
        if not children:
            return node.text
        grouped: dict[str, list[Any]] = {}
        for child in children:
            grouped.setdefault(child.tag, []).append(ExternalApiClient._xml_node(child))
        out: dict[str, Any] = {}
        for key, values in grouped.items():
            if key == "item":
                out[key] = values
            elif len(values) == 1:
                out[key] = values[0]
            else:
                out[key] = values
        if node.tag == "items":
            item = out.get("item")
            if item is None:
                return []
            return item if isinstance(item, list) else [item]
        return out
