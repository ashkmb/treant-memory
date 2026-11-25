import httpx
import pytest
import respx

from treant.embed import EmbedderUnavailable, HttpEmbedder


def _embedder(dims=768):
    return HttpEmbedder("http://emb:8080", "m", dims, "q: ", "d: ", 5000)


@respx.mock
def test_http_embedder_rejects_wrong_dims():
    respx.post("http://emb:8080/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    )
    with pytest.raises(EmbedderUnavailable):
        _embedder().embed_documents(["x"])


@respx.mock
def test_http_embedder_prefixes_truncates_and_normalises():
    route = respx.post("http://emb:8080/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})
    )
    vec = _embedder(2).embed_documents(["y" * 6000])[0]
    assert vec == pytest.approx([0.6, 0.8])
    sent = route.calls[0].request.content.decode()
    assert '"d: yyy' in sent and "y" * 5001 not in sent
