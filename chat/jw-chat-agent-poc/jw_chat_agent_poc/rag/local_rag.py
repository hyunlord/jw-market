from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


STRUCTURED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".json"}


@dataclass(frozen=True)
class RagResult:
    source: str
    summary_text: str
    chunks: list[dict]


class LocalDocumentRag:
    def __init__(self, chunk_chars: int = 480) -> None:
        self.chunk_chars = chunk_chars

    def search(self, question: str, documents: list[Path], top_k: int = 2) -> RagResult:
        chunks: list[dict] = []
        for doc in documents:
            if doc.suffix.lower() in STRUCTURED_EXTENSIONS:
                raise ValueError(f"정형 통계 업로드는 거부됩니다: {doc.name}")
            text = doc.read_text(encoding="utf-8")
            for idx, chunk in enumerate(self._chunk(text)):
                chunks.append({"document": doc.name, "chunk_id": idx, "text": chunk})
        if not chunks:
            return RagResult(source="document", summary_text="검색 가능한 문서가 없습니다.", chunks=[])
        vectorizer = TfidfVectorizer()
        matrix = vectorizer.fit_transform([c["text"] for c in chunks] + [question])
        scores = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:top_k]
        selected = [{**chunks[i], "score": round(float(score), 4)} for i, score in ranked]
        summary = " / ".join(f"{c['document']}#{c['chunk_id']}: {self._one_line(c['text'])}" for c in selected)
        return RagResult(source="document", summary_text=summary, chunks=selected)

    def _chunk(self, text: str) -> list[str]:
        compact = re.sub(r"\\s+", " ", text).strip()
        return [compact[i : i + self.chunk_chars] for i in range(0, len(compact), self.chunk_chars)] or [""]

    @staticmethod
    def _one_line(text: str) -> str:
        return re.sub(r"\\s+", " ", text).strip()[:180]

