"""Gemini プロバイダのモックテスト。

実 API との齟齬はこれでは検出できない（test_providers_gemini_live.py が担当）。
ここで固定するのは、リクエストの組み立てと応答の解釈という自前のロジック。
"""

import base64
import json
import math

import httpx
import pytest

from kb.embeddings import EmbeddingProvider
from kb.llm import LlmClient
from kb.providers import ProviderError
from kb.providers.gemini import (
    MAX_INDEXABLE_DIMENSIONS,
    GeminiEmbeddingProvider,
    GeminiLlmClient,
)

API_KEY = "test-key"


def mock_client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), headers={"x-goog-api-key": API_KEY}
    )


def embedding_response(vectors):
    return httpx.Response(200, json={"embeddings": [{"values": v} for v in vectors]})


def padded(values, dim: int) -> list[float]:
    """要求した次元数に合わせて 0 で埋める。"""
    return list(values) + [0.0] * (dim - len(values))


def stub(dim: int, values=(1.0,)):
    """要求件数ぶんの正しい次元のベクトルを返すハンドラ。"""

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["requests"])
        return embedding_response([padded(values, dim)] * count)

    return handler


# ------------------------------------------------------------------ embedding


def test_satisfies_provider_protocol():
    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, client=mock_client(stub(1536))
    )
    assert isinstance(provider, EmbeddingProvider)


def test_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return embedding_response([padded([3.0, 4.0], 1536)])

    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, model="gemini-embedding-001", dimensions=1536,
        client=mock_client(handler),
    )
    provider.embed(["ORA-01555 が発生"])

    assert seen["url"].endswith("/models/gemini-embedding-001:batchEmbedContents")
    # キーは URL ではなくヘッダに載せる（クエリ文字列はログに残りやすい）
    assert "key=" not in seen["url"]
    assert seen["key"] == API_KEY
    request = seen["body"]["requests"][0]
    assert request["model"] == "models/gemini-embedding-001"
    assert request["content"]["parts"][0]["text"] == "ORA-01555 が発生"
    assert request["output_dimensionality"] == 1536


def test_vectors_are_normalized():
    """コサイン距離で検索するため、単位長に揃える。"""
    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, dimensions=128,
        client=mock_client(lambda r: embedding_response([padded([3.0, 4.0], 128)])),
    )
    vec = provider.embed(["何か"])[0]
    assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)
    assert math.isclose(vec[0], 0.6, rel_tol=1e-9)


def test_output_order_and_length_match_input():
    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["requests"])
        return embedding_response(
            [padded([float(i + 1)], 128) for i in range(count)]
        )

    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, dimensions=128, client=mock_client(handler)
    )
    assert len(provider.embed(["a", "b", "c"])) == 3
    assert provider.embed([]) == []


def test_empty_text_skips_the_api():
    """空文字は API に投げない。エラーになるうえ無駄な課金にもなる。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(len(body["requests"]))
        return embedding_response([padded([1.0], 128)] * len(body["requests"]))

    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, dimensions=128, client=mock_client(handler)
    )
    vectors = provider.embed(["", "   ", "本文"])

    assert calls == [1]  # 空でない 1 件だけ
    assert len(vectors) == 3
    for vec in vectors:
        # ゼロベクトルはコサイン距離が定義できないので、単位ベクトルで埋める
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


def test_batches_are_split():
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["requests"])
        sizes.append(count)
        return embedding_response([padded([1.0], 128)] * count)

    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, dimensions=128, batch_size=2, client=mock_client(handler)
    )
    provider.embed([f"text {i}" for i in range(5)])
    assert sizes == [2, 2, 1]


def test_dimension_mismatch_is_rejected():
    """次元数が食い違ったまま格納すると、検索時に初めて壊れる。"""
    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, dimensions=768,
        client=mock_client(lambda r: embedding_response([[1.0] * 512])),
    )
    with pytest.raises(ProviderError, match="次元"):
        provider.embed(["本文"])


def test_missing_vectors_are_rejected():
    provider = GeminiEmbeddingProvider(
        api_key=API_KEY, client=mock_client(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(ProviderError):
        provider.embed(["a"])


def test_dimensions_above_index_limit_are_rejected():
    """HNSW を張れない次元数は、取り込み後ではなく設定時に弾く。"""
    with pytest.raises(ValueError, match=str(MAX_INDEXABLE_DIMENSIONS)):
        GeminiEmbeddingProvider(api_key=API_KEY, dimensions=3072)


def test_dimensions_outside_api_range_are_rejected():
    with pytest.raises(ValueError):
        GeminiEmbeddingProvider(api_key=API_KEY, dimensions=64)


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        GeminiEmbeddingProvider(api_key="")


def test_rate_limit_is_reported_clearly():
    provider = GeminiEmbeddingProvider(
        api_key=API_KEY,
        client=mock_client(lambda r: httpx.Response(429, text="slow down")),
    )
    with pytest.raises(ProviderError, match="レート制限"):
        provider.embed(["a"])


def test_connection_failure_becomes_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = GeminiEmbeddingProvider(api_key=API_KEY, client=mock_client(handler))
    with pytest.raises(ProviderError, match="接続"):
        provider.embed(["a"])


# ----------------------------------------------------------------------- LLM


def text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]}
    )


def test_llm_satisfies_protocol():
    assert isinstance(
        GeminiLlmClient(api_key=API_KEY, client=mock_client(lambda r: text_response(""))),
        LlmClient,
    )


def test_image_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return text_response("ORA-01555")

    llm = GeminiLlmClient(
        api_key=API_KEY, model="gemini-2.5-flash", client=mock_client(handler)
    )
    assert llm.extract_text_from_image(b"\x89PNG...", mime_type="image/png") == "ORA-01555"

    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    parts = seen["body"]["contents"][0]["parts"]
    assert "文字" in parts[0]["text"]  # 説明ではなく文字起こしを指示している
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"\x89PNG..."


def test_charset_suffix_in_mime_type_is_tolerated():
    llm = GeminiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: text_response("あ"))
    )
    assert llm.extract_text_from_image(b"x", mime_type="image/PNG; charset=binary") == "あ"


def test_unsupported_image_type_is_rejected():
    llm = GeminiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: text_response(""))
    )
    with pytest.raises(ProviderError, match="扱えない"):
        llm.extract_text_from_image(b"GIF89a", mime_type="image/gif")


def test_oversized_image_is_rejected():
    llm = GeminiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: text_response(""))
    )
    with pytest.raises(ProviderError, match="大きすぎ"):
        llm.extract_text_from_image(b"x" * (15 * 1024 * 1024), mime_type="image/png")


def test_no_candidates_means_no_text():
    """文字が写っていない画像。取り込み全体を止めるほどのことではない。"""
    llm = GeminiLlmClient(
        api_key=API_KEY,
        client=mock_client(lambda r: httpx.Response(200, json={"candidates": []})),
    )
    assert llm.extract_text_from_image(b"x", mime_type="image/png") == ""


def test_blocked_prompt_is_reported():
    llm = GeminiLlmClient(
        api_key=API_KEY,
        client=mock_client(
            lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        ),
    )
    with pytest.raises(ProviderError, match="SAFETY"):
        llm.extract_text_from_image(b"x", mime_type="image/png")
