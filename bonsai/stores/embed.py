"""Pluggable embedding providers.

Three backends:
  hash     — 256-dim deterministic hash embedding (no ML, for MVP / testing)
  openai   — OpenAI-compatible embeddings endpoint
  local    — sentence-transformers (bge-m3 or similar)

The `hash` backend is intentionally weak but lets the whole pipeline run
without installing any ML. Switch to openai/local via config when available.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


_TOKEN_RE = re.compile(r"\w+")


@dataclass
class HashEmbedder:
    """Deterministic ~256-dim bag-of-hashes embedding.

    Good enough as a sanity-check vector channel; real semantic retrieval
    should switch to a real embedder. Combined with BM25 in hybrid search.
    """

    dim: int = 256
    name: str = "hash"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for tok in _TOKEN_RE.findall(t.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                idx = h % self.dim
                sign = 1 if (h >> 16) & 1 else -1
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@dataclass
class OpenAIEmbedder:
    """OpenAI-compatible /embeddings endpoint (works with OpenAI, Zhipu, Qwen...)."""

    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "text-embedding-3-small"
    dim: int = 1536
    name: str = "openai"
    timeout: float = 60.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        r = httpx.post(
            f"{self.base_url.rstrip('/')}/embeddings",
            json={"model": self.model, "input": texts},
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        vecs = [d["embedding"] for d in data["data"]]
        if vecs and len(vecs[0]) != self.dim:
            self.dim = len(vecs[0])   # 纠正 bge-m3=1024 之类的实际维度
        return vecs


@dataclass
class LocalEmbedder:
    """sentence-transformers (bge-m3 etc). Lazy import; heavy."""

    model_name: str = "BAAI/bge-m3"
    name: str = "local"
    dim: int = 1024
    _model = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            self.dim = self._model.get_sentence_embedding_dimension()
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vecs]


def build_embedder(cfg: dict) -> Embedder:
    provider = (cfg.get("embed_provider") or "hash").lower()
    if provider == "hash":
        return HashEmbedder()
    if provider == "openai":
        return OpenAIEmbedder(
            api_key=cfg.get("embed_api_key", ""),
            base_url=cfg.get("embed_base_url", "https://api.openai.com/v1"),
            model=cfg.get("embed_model", "text-embedding-3-small"),
        )
    if provider == "local":
        return LocalEmbedder(model_name=cfg.get("embed_model", "BAAI/bge-m3"))
    raise ValueError(f"unknown embed_provider: {provider}")
