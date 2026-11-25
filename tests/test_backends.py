import json

import httpx
import pytest

from treant import backends
from treant.settings import WorkspaceSettings


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_litellm_backend_posts_alias_and_bearer():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return _reply("hello")

    b = backends.LiteLLMBackend("http://llm/v1/", "sk-secret", _client(handler))
    out = b.complete([{"role": "user", "content": "hi"}], alias="fast")
    assert out == "hello"
    assert seen["url"] == "http://llm/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["model"] == "fast" and seen["body"]["temperature"] == 0
    assert "sk-secret" not in repr(b)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_litellm_backend_unavailable_on_5xx_and_timeout(status):
    b = backends.LiteLLMBackend("http://llm", "k", _client(lambda r: httpx.Response(status)))
    with pytest.raises(backends.BackendUnavailable):
        b.complete([], alias="a")

    def boom(req):
        raise httpx.ConnectTimeout("t")

    with pytest.raises(backends.BackendUnavailable):
        backends.LiteLLMBackend("http://llm", "k", _client(boom)).complete([], alias="a")


def test_litellm_backend_rejected_and_empty():
    b = backends.LiteLLMBackend(
        "http://llm", "k", _client(lambda r: httpx.Response(400, text="body-secret"))
    )
    with pytest.raises(backends.BackendRejected, match="rejected") as e:
        b.complete([], alias="a")
    assert "body-secret" not in str(e.value)
    for resp in (
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        httpx.Response(200, text="not json"),
    ):
        b = backends.LiteLLMBackend("http://llm", "k", _client(lambda r, resp=resp: resp))
        with pytest.raises(backends.BackendUnavailable, match="empty reply"):
            b.complete([], alias="a")


def test_for_workspace_none_and_missing_key():
    assert backends.for_workspace({}, "alpha", WorkspaceSettings(backend="none")) is None
    s = WorkspaceSettings()
    with pytest.raises(backends.BackendUnavailable):
        backends.for_workspace({"TREANT_LITELLM_URL": "http://x"}, "alpha", s)
    with pytest.raises(backends.BackendUnavailable):
        backends.for_workspace({"TREANT_LITELLM_KEY_ALPHA": "k"}, "alpha", s)
    env = {"TREANT_LITELLM_URL": "http://x", "TREANT_LITELLM_KEY_ALPHA": "k"}
    assert isinstance(backends.for_workspace(env, "alpha", s), backends.LiteLLMBackend)
    with pytest.raises(backends.BackendUnavailable):
        backends.for_workspace(env, "beta", s)


def _jev(handler):
    return backends.JevJudge("https://jev/", "jev-1", "jk", _client(handler))


def test_jev_score_and_errors():
    seen = {}

    def ok(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"answers": {"general": {"noul": 0.7}}})

    assert _jev(ok).score("text", "is it good?") == 0.7
    assert seen["url"] == "https://jev/v1/systemone" and seen["auth"] == "Bearer jk"
    q = seen["body"]["questions"]["general"]
    assert q["type"] == "noul" and q["instructions"] == "is it good?"
    assert seen["body"]["state"] == "text" and seen["body"]["model"] == "jev-1"
    assert "jk" not in repr(_jev(ok))

    bad = [
        httpx.Response(500),
        httpx.Response(429),
        httpx.Response(401, text="leak"),
        httpx.Response(200, text="nope"),
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, json={"answers": {"general": {"noul": 1.5}}}),
        httpx.Response(200, json={"answers": {"general": {"noul": True}}}),
    ]
    for resp in bad:
        with pytest.raises(backends.BackendUnavailable) as e:
            _jev(lambda r, resp=resp: resp).score("t", "q")
        assert "leak" not in str(e.value)

    def boom(req):
        raise httpx.ReadTimeout("t")

    with pytest.raises(backends.BackendUnavailable):
        _jev(boom).score("t", "q")


def test_jev_4xx_is_rejected_but_5xx_is_not():
    with pytest.raises(backends.BackendRejected):
        _jev(lambda r: httpx.Response(403)).score("t", "q")
    for status in (429, 500):
        with pytest.raises(backends.BackendUnavailable) as e:
            _jev(lambda r, s=status: httpx.Response(s)).score("t", "q")
        assert not isinstance(e.value, backends.BackendRejected)


def test_litellm_backend_sends_max_tokens_default_and_override():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["max_tokens"])
        return _reply("ok")

    b = backends.LiteLLMBackend("http://llm", "k", _client(handler))
    b.complete([], alias="a")
    b.complete([], alias="a", max_tokens=200)
    small = backends.LiteLLMBackend("http://llm", "k", _client(handler), max_tokens=1000)
    small.complete([], alias="a")
    assert seen == [backends.MAX_TOKENS, 200, 1000]


def test_litellm_empty_content_is_a_bad_reply_not_an_outage():
    for content in ("", "   ", None):
        resp = httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        b = backends.LiteLLMBackend("http://llm", "k", _client(lambda r, resp=resp: resp))
        with pytest.raises(backends.BackendBadReply):
            b.complete([], alias="a")
    assert issubclass(backends.BackendBadReply, backends.BackendUnavailable)
    assert not issubclass(backends.BackendBadReply, backends.BackendRejected)
