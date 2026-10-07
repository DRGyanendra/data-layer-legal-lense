"""Vector stores: Qdrant for real use, an in-memory one for tests.

Both expose the same four methods, so the pipeline does not care which
it is talking to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Point:
    id: str             # UUID string
    vector: list[float]
    payload: dict


@dataclass
class Hit:
    score: float
    payload: dict


class VectorStore(Protocol):
    def ensure_collection(self) -> None: ...
    def find_duplicate(self, doc_sha256: str, case_id: str) -> str | None: ...
    def replace_case(self, case_id: str, points: list[Point]) -> int: ...
    def search(self, vector: list[float], *, limit: int = 10, statute: str | None = None,
               case_id: str | None = None, substantive_only: bool = True) -> list[Hit]: ...


class QdrantStore:
    """judgment_chunks in Qdrant. Requires qdrant-client 1.10 or newer."""

    # Fields the backend filters on. 'statutes' and 'statutes_current' are what
    # make exact section lookups ('IPC:304A') work without a keyword engine.
    KEYWORD_FIELDS = ["case_id", "court", "statutes", "statutes_current", "opinion_type",
                      "rhetorical_role", "doc_sha256", "para_numbering", "doc_type", "parse_quality", "meta_source",
                      "block_id", "parent_id", "chunk_id", "cited_cases"]
    INTEGER_FIELDS = ["paragraph_num", "para_end"]

    def __init__(self, url: str, api_key: str | None, collection: str, dim: int):
        from qdrant_client import QdrantClient

        self.client = QdrantClient(url=url, api_key=api_key, timeout=60)
        self.collection = collection
        self.dim = dim

    def ensure_collection(self) -> None:
        from qdrant_client import models

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE),
            )
        else:
            info = self.client.get_collection(self.collection)
            size = info.config.params.vectors.size
            if size != self.dim:
                raise RuntimeError(
                    f"Collection {self.collection} holds {size}-dimension vectors but the "
                    f"embedder produces {self.dim}. Use a new collection name."
                )
        for name in self.KEYWORD_FIELDS:
            self.client.create_payload_index(self.collection, name, models.PayloadSchemaType.KEYWORD)
        for name in self.INTEGER_FIELDS:
            self.client.create_payload_index(self.collection, name, models.PayloadSchemaType.INTEGER)
        self.client.create_payload_index(self.collection, "date", models.PayloadSchemaType.DATETIME)
        self.client.create_payload_index(self.collection, "substantive", models.PayloadSchemaType.BOOL)
        self.client.create_payload_index(
            self.collection, "text",
            models.TextIndexParams(type=models.TextIndexType.TEXT,
                                   tokenizer=models.TokenizerType.WORD, lowercase=True),
        )

    def find_duplicate(self, doc_sha256: str, case_id: str) -> str | None:
        from qdrant_client import models

        points, _ = self.client.scroll(
            self.collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="doc_sha256", match=models.MatchValue(value=doc_sha256))],
                must_not=[models.FieldCondition(key="case_id", match=models.MatchValue(value=case_id))],
            ),
            limit=1, with_payload=["case_id"],
        )
        return points[0].payload["case_id"] if points else None

    def replace_case(self, case_id: str, points: list[Point]) -> int:
        from qdrant_client import models

        # Remove the earlier load first, so chunks that no longer exist do not linger.
        self.client.delete(
            self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="case_id", match=models.MatchValue(value=case_id))]
                )
            ),
            wait=True,
        )
        for start in range(0, len(points), 64):
            batch = points[start:start + 64]
            self.client.upsert(
                self.collection,
                points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in batch],
                wait=True,
            )
        return len(points)

    def search(self, vector, *, limit=10, statute=None, case_id=None, substantive_only=True) -> list[Hit]:
        from qdrant_client import models

        must = []
        if substantive_only:
            must.append(models.FieldCondition(key="substantive", match=models.MatchValue(value=True)))
        if statute:
            must.append(
                models.Filter(should=[
                    models.FieldCondition(key="statutes", match=models.MatchValue(value=statute)),
                    models.FieldCondition(key="statutes_current", match=models.MatchValue(value=statute)),
                ])
            )
        if case_id:
            must.append(models.FieldCondition(key="case_id", match=models.MatchValue(value=case_id)))
        result = self.client.query_points(
            self.collection, query=vector, limit=limit, with_payload=True,
            query_filter=models.Filter(must=must) if must else None,
        )
        return [Hit(p.score, p.payload) for p in result.points]


class MemoryStore:
    """Brute-force cosine search over a Python list. For tests and dry runs."""

    def __init__(self) -> None:
        self.points: dict[str, Point] = {}

    def ensure_collection(self) -> None:
        pass

    def find_duplicate(self, doc_sha256: str, case_id: str) -> str | None:
        for p in self.points.values():
            if p.payload.get("doc_sha256") == doc_sha256 and p.payload.get("case_id") != case_id:
                return p.payload["case_id"]
        return None

    def replace_case(self, case_id: str, points: list[Point]) -> int:
        self.points = {k: v for k, v in self.points.items() if v.payload.get("case_id") != case_id}
        for p in points:
            self.points[p.id] = p
        return len(points)

    def search(self, vector, *, limit=10, statute=None, case_id=None, substantive_only=True) -> list[Hit]:
        hits = []
        for p in self.points.values():
            if substantive_only and not p.payload.get("substantive", True):
                continue
            if case_id and p.payload.get("case_id") != case_id:
                continue
            if statute and statute not in p.payload.get("statutes", []) \
                    and statute not in p.payload.get("statutes_current", []):
                continue
            score = sum(a * b for a, b in zip(vector, p.vector))
            hits.append(Hit(score, p.payload))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]
