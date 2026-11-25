"""Model backends (a LiteLLM proxy) and the Jev judge.

Every failure is ``BackendUnavailable`` with a fixed phrase: never a response body, a prompt,
a reply or a key. Callers treat it as "wait and retry", never as a pass. Two subclasses mark
failures a retry is unlikely to fix: ``BackendRejected`` (a 4xx) and ``BackendBadReply`` (the
model answered with nothing usable); callers cap those retries low.
"""

from collections.abc import Mapping
from typing import Protocol

import httpx

from treant.settings import WorkspaceSettings

TIMEOUT_S = 60.0
JEV_TIMEOUT_S = 20.0
# Reasoning models spend part of max_tokens on hidden reasoning; too low a cap gives an
# empty reply (seen live with the judge at 200).
MAX_TOKENS = 16000


class BackendUnavailable(Exception):
    """The model service could not give a usable answer; the item must wait, not pass."""


class BackendRejected(BackendUnavailable):
    """A non-429 4xx: the request or key was refused (not an outage). Still never a pass."""


class BackendBadReply(BackendUnavailable):
    """The model answered, but the answer is not usable (empty, not the asked-for JSON).

    Retrying repeats the same deterministic prompt, so callers cap these retries low.
    """


class Backend(Protocol):
    def complete(
        self, messages: list[dict], *, alias: str, max_tokens: int | None = None
    ) -> str: ...

    # Implementations that hold a client also offer ``close()``; callers use it when present.


class LiteLLMBackend:
    def __init__(
        self,
        base_url: str,
        key: str,
        http: httpx.Client | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = key
        self._max_tokens = max_tokens
        self._own = http is None
        self._http = http or httpx.Client(timeout=TIMEOUT_S)

    def close(self) -> None:
        """Close the HTTP client this backend created (a passed-in client stays open)."""
        if self._own:
            self._http.close()

    def __repr__(self) -> str:
        return "LiteLLMBackend(<redacted>)"

    def complete(self, messages: list[dict], *, alias: str, max_tokens: int | None = None) -> str:
        """The reply text. ``max_tokens`` overrides the constructor's cap for this call."""
        body = {
            "model": alias,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens or self._max_tokens,
        }
        try:
            resp = self._http.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=TIMEOUT_S,
            )
        except httpx.TimeoutException:
            raise BackendUnavailable("model request timed out") from None
        except httpx.HTTPError:
            raise BackendUnavailable("model request failed") from None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise BackendUnavailable("model service unavailable")
        if resp.status_code != 200:
            raise BackendRejected("rejected")
        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise BackendUnavailable("empty reply") from None
        if not isinstance(text, str) or not text.strip():
            raise BackendBadReply("empty reply")
        return text


def for_workspace(env: Mapping[str, str], ws: str, s: WorkspaceSettings) -> Backend | None:
    if s.backend == "none":
        return None
    url = env.get("TREANT_LITELLM_URL")
    key = env.get("TREANT_LITELLM_KEY_" + ws.upper())
    if not url or not key:
        raise BackendUnavailable("model backend is not configured")
    return LiteLLMBackend(url, key)


class JevJudge:
    def __init__(self, url: str, model: str, key: str, http: httpx.Client | None = None) -> None:
        self._url = url.rstrip("/") + "/v1/systemone"
        self._model = model
        self._key = key
        self._own = http is None
        self._http = http or httpx.Client(timeout=JEV_TIMEOUT_S)

    def close(self) -> None:
        """Close the HTTP client this judge created (a passed-in client stays open)."""
        if self._own:
            self._http.close()

    def __repr__(self) -> str:
        return "JevJudge(<redacted>)"

    def score(self, text: str, question: str) -> float:
        body = {
            "model": self._model,
            "state": text,
            "questions": {"general": {"type": "noul", "instructions": question}},
        }
        try:
            resp = self._http.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=JEV_TIMEOUT_S,
            )
        except httpx.TimeoutException:
            raise BackendUnavailable("jev request timed out") from None
        except httpx.HTTPError:
            raise BackendUnavailable("jev request failed") from None
        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            raise BackendRejected("rejected")
        if resp.status_code != 200:
            raise BackendUnavailable("jev returned an error status")
        try:
            value = resp.json()["answers"]["general"]["noul"]
        except (ValueError, KeyError, TypeError):
            raise BackendUnavailable("jev reply had no answer") from None
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
            raise BackendUnavailable("jev answer was out of range")
        return float(value)
