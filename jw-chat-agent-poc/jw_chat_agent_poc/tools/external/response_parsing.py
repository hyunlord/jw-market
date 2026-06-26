from __future__ import annotations

from typing import Any
import xml.etree.ElementTree as ET

import requests


def parse_response(response: requests.Response, xml: bool) -> dict[str, Any]:
    if xml:
        return parse_xml(response.text)
    return response.json()


def render_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


def summary(tool: str, status_code: int, payload: dict[str, Any]) -> str:
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


def parse_xml(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    return _xml_node(root)


def _xml_node(node: ET.Element) -> Any:
    children = list(node)
    if not children:
        return node.text
    grouped: dict[str, list[Any]] = {}
    for child in children:
        grouped.setdefault(child.tag, []).append(_xml_node(child))
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
