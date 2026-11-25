"""Embedding client for the llama.cpp sidecar (OpenAI-compatible /v1/embeddings)."""

import math
from typing import Protocol

import httpx


class EmbedderUnavailable(Exception):
    """The embedding backend is down, slow, or returned something unusable."""


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def ping(self, timeout_s: float = 5) -> bool: ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0 or not math.isfinite(norm):
        raise EmbedderUnavailable("embedding backend returned an unusable vector")
    return [x / norm for x in vec]


class HttpEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        dims: int = 768,
        query_prefix: str = "task: search result | query: ",
        doc_prefix: str = "title: none | text: ",
        max_chars: int = 5000,
        timeout_s: float = 120,
        query_timeout_s: float = 15,
    ):
        self._url = f"{base_url.rstrip('/')}/v1/embeddings"
        self._model = model
        self._dims = dims
        self._query_prefix = query_prefix
        self._doc_prefix = doc_prefix
        self._max_chars = max_chars
        self._timeout_s = timeout_s
        # Short: a query waiting behind an index batch should fail fast.
        self._query_timeout_s = query_timeout_s
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def _post(self, inputs: list[str], timeout_s: float) -> list[list[float]]:
        try:
            resp = self._client.post(
                self._url, json={"model": self._model, "input": inputs}, timeout=timeout_s
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            if not isinstance(data, list) or len(data) != len(inputs):
                raise ValueError("count mismatch")
            by_index = {int(item["index"]): item["embedding"] for item in data}
            if sorted(by_index) != list(range(len(inputs))):
                raise ValueError("bad indexes")
            vectors = [[float(x) for x in by_index[i]] for i in range(len(inputs))]
            if any(len(v) != self._dims for v in vectors):
                raise EmbedderUnavailable("embedding backend returned the wrong dimension")
            return [_normalise(v) for v in vectors]
        except EmbedderUnavailable:
            raise
        except httpx.HTTPStatusError as e:
            raise EmbedderUnavailable(
                f"embedding backend returned HTTP {e.response.status_code}"
            ) from e
        except httpx.HTTPError as e:
            raise EmbedderUnavailable(f"embedding request failed: {type(e).__name__}") from e
        except (ValueError, KeyError, TypeError) as e:
            raise EmbedderUnavailable(f"malformed embedding response: {type(e).__name__}") from e

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        inputs = [self._doc_prefix + t[: self._max_chars] for t in texts]
        return self._post(inputs, self._timeout_s) if inputs else []

    def embed_query(self, text: str) -> list[float]:
        return self._post([self._query_prefix + text[: self._max_chars]], self._query_timeout_s)[0]

    def ping(self, timeout_s: float = 5) -> bool:
        try:
            self._post(["ping"], timeout_s)
            return True
        except Exception:  # noqa: BLE001 - ping must never raise
            return False
