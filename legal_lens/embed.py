"""Embedding models.

BgeM3Embedder is the real one. HashEmbedder needs no download and gives
rough word-overlap similarity; it exists for tests and dry runs and must
never be used to load the shared Qdrant collection.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class BgeM3Embedder:
    """Dense 1024-dimension vectors from BAAI/bge-m3, L2-normalised.

    The backend must embed queries with this same model and the same
    normalisation, or search results will be meaningless.
    """

    def __init__(self, model_name: str = "BAAI/bge-m3", batch_size: int = 8):
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self.batch_size = batch_size
        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 32,
        )
        return [v.tolist() for v in vectors]


class HashEmbedder:
    """Deterministic bag-of-words vectors. For tests and dry runs only."""

    def __init__(self, dim: int = 256):
        self.name = f"hash-{dim}"
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                digest = hashlib.md5(word.encode()).digest()
                vec[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors
