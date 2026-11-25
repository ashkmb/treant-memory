import hashlib
import math
import re

from treant.embed import EmbedderUnavailable

DIMS = 768
_TOKEN = re.compile(r"\w+")


class FakeEmbedder:
    """Deterministic hashed bag-of-words embedder; related texts share dimensions."""

    def __init__(self, down: bool = False):
        self.down = down

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIMS
        for tok in _TOKEN.findall(text.lower()):
            h = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            v[int.from_bytes(h, "big") % DIMS] += 1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            v[0] = 1.0
            return v
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.down:
            raise EmbedderUnavailable("fake embedder is down")
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        if self.down:
            raise EmbedderUnavailable("fake embedder is down")
        return self._vec(text)

    def ping(self, timeout_s: float = 5) -> bool:
        return not self.down


class FakeBackend:
    """Scripted model backend: a list of replies (consumed in order), or a callable.

    ``unavailable=True`` makes every call raise ``BackendUnavailable``. Calls are recorded in
    ``calls`` as ``(messages, alias)``; the ``max_tokens`` of each call in ``max_tokens``.
    """

    def __init__(self, replies=None, unavailable: bool = False):
        self.replies = replies if replies is not None else []
        self.unavailable = unavailable
        self.calls: list[tuple[list[dict], str]] = []
        self.max_tokens: list[int | None] = []

    @property
    def messages(self) -> list[list[dict]]:
        return [m for m, _ in self.calls]

    def complete(self, messages: list[dict], *, alias: str, max_tokens: int | None = None) -> str:
        from treant.backends import BackendUnavailable

        self.calls.append((messages, alias))
        self.max_tokens.append(max_tokens)
        if self.unavailable:
            raise BackendUnavailable("fake backend is down")
        if callable(self.replies):
            return self.replies(messages, alias)
        if not self.replies:
            raise BackendUnavailable("no scripted reply")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeJev:
    """Scripted Jev judge: a score, a list of scores, or ``unavailable=True`` to raise."""

    def __init__(self, score=1.0, unavailable: bool = False):
        self.scores = list(score) if isinstance(score, list | tuple) else None
        self.value = score
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    def score(self, text: str, question: str) -> float:
        from treant.backends import BackendUnavailable

        self.calls.append((text, question))
        if self.unavailable:
            raise BackendUnavailable("fake jev is down")
        if self.scores is not None:
            return self.scores.pop(0)
        return float(self.value)
