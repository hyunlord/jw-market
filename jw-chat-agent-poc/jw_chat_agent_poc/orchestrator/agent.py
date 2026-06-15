from __future__ import annotations

from pathlib import Path
from typing import Any

from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall
from jw_chat_agent_poc.tools.metrics import MetricsTool


class ChatAgent:
    def __init__(
        self,
        external_mode: str = "fixture",
        router: BQRouter | None = None,
        resolver: BrandResolver | None = None,
        metrics: MetricsTool | None = None,
        external: ExternalApiClient | None = None,
        rag: LocalDocumentRag | None = None,
    ) -> None:
        self.router = router or BQRouter()
        self.resolver = resolver or BrandResolver()
        self.metrics = metrics or MetricsTool()
        self.external = external or ExternalApiClient(mode=external_mode)
        self.rag = rag or LocalDocumentRag()

    def answer(self, question: str, documents: list[Path] | None = None) -> dict[str, Any]:
        docs = documents or []
        resolution = self.resolver.resolve(question)
        routes = self.router.route(question, has_documents=bool(docs))
        calls: list[dict[str, Any]] = []
        summaries: list[str] = []
        sources: list[str] = []

        if any("none" in route.sources for route in routes):
            return self._no_data(question, resolution, routes)

        if any("metrics" in route.sources for route in routes):
            market = "ml_006" if resolution.canonical_brand in {"리바로", "리바로젯"} else "mock_market"
            landscape = self.metrics.get_market_landscape(market)
            brand_metric = self.metrics.get_brand_metric(resolution.canonical_brand)
            for call in (landscape, brand_metric):
                calls.append(call)
                summaries.append(call["summary_text"])
                sources.append(call["source"])

        if any("external_api" in route.sources for route in routes):
            external_calls = self._external_calls(question, resolution)
            for call in external_calls:
                calls.append(call.__dict__)
                summaries.append(call.summary_text)
                sources.append(call.source)

        if docs and any("document" in route.sources for route in routes):
            rag_result = self.rag.search(question, docs)
            calls.append({"tool": "document_rag", **rag_result.__dict__})
            summaries.append(rag_result.summary_text)
            sources.append(rag_result.source)

        answer = self._compose_answer(question, resolution.canonical_brand, summaries, sources)
        return {
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "tool_calls": calls,
            "answer": answer,
            "sources": sorted(set(sources)),
        }

    def _external_calls(self, question: str, resolution) -> list[ExternalCall]:
        lower = question.lower()
        calls: list[ExternalCall] = []
        if "임상" in question or "clinical" in lower:
            calls.append(self.external.clinicaltrials_v2_search(" OR ".join(resolution.molecule_en)))
            calls.append(self.external.mfds_clinical_trial_kr(resolution.canonical_brand))
        if "fda" in lower or "라벨" in question or "label" in lower:
            for molecule in resolution.molecule_en:
                calls.append(self.external.openfda_label_search(molecule))
        if "특허" in question or "patent" in lower or "orange" in lower:
            for molecule in resolution.molecule_en:
                calls.append(self.external.mfds_patent(molecule))
                calls.append(self.external.mfds_fda_orangebook(molecule))
        if not calls:
            calls.append(self.external.mfds_permission_search(resolution.canonical_brand))
        return calls

    @staticmethod
    def _compose_answer(question: str, brand: str, summaries: list[str], sources: list[str]) -> str:
        source_label = ", ".join(sorted(set(sources)))
        joined = " ".join(summaries)
        return f"{brand} 질문에 대해 {source_label} 근거를 종합했습니다. {joined}"

    @staticmethod
    def _no_data(question: str, resolution, routes) -> dict[str, Any]:
        return {
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "tool_calls": [],
            "answer": "현재 데이터로 답변 불가합니다. Q4 영업 Impact 또는 Q5 포트폴리오·사업성 영역은 P1 POC 데이터 범위 밖입니다.",
            "sources": ["none"],
        }

