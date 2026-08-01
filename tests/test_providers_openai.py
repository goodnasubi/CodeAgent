"""OpenAI プロバイダのモックテスト。

実 API との齟齬はこれでは検出できない（test_providers_openai_live.py が担当）。
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
from kb.providers.openai import (
    MAX_INDEXABLE_DIMENSIONS,
    OpenAiEmbeddingProvider,
    OpenAiLlmClient,
)

API_KEY = "test-key"


def mock_client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {API_KEY}"},
    )


def embedding_response(vectors, *, order=None):
    """OpenAI 形式の応答。order で data の並び順を入れ替えられる。"""
    items = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    if order is not None:
        items = [items[i] for i in order]
    return httpx.Response(200, json={"data": items, "model": "test"})


def padded(values, dim: int) -> list[float]:
    """要求した次元数に合わせて 0 で埋める。"""
    return list(values) + [0.0] * (dim - len(values))


def stub(dim: int, values=(1.0,)):
    """要求件数ぶんの正しい次元のベクトルを返すハンドラ。"""

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["input"])
        return embedding_response([padded(values, dim)] * count)

    return handler


# ------------------------------------------------------------------ embedding


def test_satisfies_provider_protocol():
    provider = OpenAiEmbeddingProvider(api_key=API_KEY, client=mock_client(stub(1536)))
    assert isinstance(provider, EmbeddingProvider)


def test_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return embedding_response([padded((1.0,), 256)])

    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=256, model="m", client=mock_client(handler)
    ) as p:
        p.embed(["こんにちは"])

    assert seen["url"].endswith("/embeddings")
    # キーはヘッダに載せる。クエリ文字列はログに残りやすい
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["input"] == ["こんにちは"]
    assert seen["body"]["dimensions"] == 256


def test_vectors_are_normalized():
    """次元を削ると正規化されずに返ることがある。検索はコサイン距離。"""
    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=4, client=mock_client(stub(4, (3.0, 4.0)))
    ) as p:
        vec = p.embed(["x"])[0]
    assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


def test_output_order_and_length_match_input():
    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=8, client=mock_client(stub(8))
    ) as p:
        assert len(p.embed(["a", "b", "c"])) == 3


def test_response_order_is_not_trusted():
    """**data が入力順で返るとは限らない。** index に従って並べ直す。

    ここがずれると、別の知識のベクトルを保存してしまう。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # 3 件を逆順で返す
        return embedding_response(
            [padded((1.0,), 4), padded((0.0, 1.0), 4), padded((0.0, 0.0, 1.0), 4)],
            order=[2, 0, 1],
        )

    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=4, client=mock_client(handler)
    ) as p:
        vectors = p.embed(["a", "b", "c"])

    assert vectors[0][0] == 1.0
    assert vectors[1][1] == 1.0
    assert vectors[2][2] == 1.0


def test_duplicate_index_is_rejected():
    """同じ index が 2 回来たら、欠けた入力があるということ。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": padded((1.0,), 4)},
                    {"index": 0, "embedding": padded((1.0,), 4)},
                ]
            },
        )

    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=4, client=mock_client(handler)
    ) as p:
        with pytest.raises(ProviderError):
            p.embed(["a", "b"])


def test_empty_text_skips_the_api():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["input"])
        return embedding_response([padded((1.0,), 4)] * len(payload["input"]))

    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=4, client=mock_client(handler)
    ) as p:
        vectors = p.embed(["", "   ", "本文"])

    assert calls == [["本文"]], "空文字を API に投げている"
    assert len(vectors) == 3
    # ゼロベクトルはコサイン距離が定義できない
    for v in vectors:
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_batches_are_split():
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        sizes.append(len(texts))
        return embedding_response([padded((1.0,), 4)] * len(texts))

    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=4, batch_size=2, client=mock_client(handler)
    ) as p:
        p.embed(["a", "b", "c", "d", "e"])

    assert sizes == [2, 2, 1]


def test_dimension_mismatch_is_rejected():
    """食い違ったまま格納すると、検索時に初めて壊れる。"""
    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=16, client=mock_client(stub(8))
    ) as p:
        with pytest.raises(ProviderError):
            p.embed(["x"])


def test_wrong_count_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return embedding_response([padded((1.0,), 4)])

    with OpenAiEmbeddingProvider(
        api_key=API_KEY, dimensions=4, client=mock_client(handler)
    ) as p:
        with pytest.raises(ProviderError):
            p.embed(["a", "b"])


def test_dimensions_above_index_limit_are_rejected():
    """pgvector の HNSW を作れない次元数は、設定した時点で弾く。"""
    with pytest.raises(ValueError):
        OpenAiEmbeddingProvider(
            api_key=API_KEY, dimensions=MAX_INDEXABLE_DIMENSIONS + 1
        )


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        OpenAiEmbeddingProvider(api_key="")


def test_declares_its_own_max_distance():
    """足切りはプロバイダごとの値。ハッシュ実装の 0.85 を流用しない。"""
    with OpenAiEmbeddingProvider(api_key=API_KEY, client=mock_client(stub(1536))) as p:
        assert p.max_distance < 0.85
    with OpenAiEmbeddingProvider(
        api_key=API_KEY, max_distance=0.5, client=mock_client(stub(1536))
    ) as p:
        assert p.max_distance == 0.5


def test_rate_limit_is_reported_clearly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"message": "slow down", "type": "rate_limit_exceeded"}}
        )

    with OpenAiEmbeddingProvider(api_key=API_KEY, client=mock_client(handler)) as p:
        with pytest.raises(ProviderError, match="レート制限"):
            p.embed(["x"])


def test_out_of_credit_is_not_reported_as_rate_limit():
    """**残高不足も 429 で返る。** 「時間をおいて再試行」と案内すると、
    永久に直らないものを待たせることになる。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "You have no credits remaining.",
                    "type": "insufficient_quota",
                    "code": "credit_balance_exhausted",
                }
            },
        )

    with OpenAiEmbeddingProvider(api_key=API_KEY, client=mock_client(handler)) as p:
        with pytest.raises(ProviderError, match="残高") as exc:
            p.embed(["x"])
    assert "レート制限" not in str(exc.value)


def test_malformed_error_body_does_not_crash():
    """エラー応答が JSON でないこともある。判定でさらに落ちない。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="<html>429</html>")

    with OpenAiEmbeddingProvider(api_key=API_KEY, client=mock_client(handler)) as p:
        with pytest.raises(ProviderError):
            p.embed(["x"])


def test_connection_failure_becomes_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with OpenAiEmbeddingProvider(api_key=API_KEY, client=mock_client(handler)) as p:
        with pytest.raises(ProviderError):
            p.embed(["x"])


# ------------------------------------------------------------------------ LLM

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def chat_response(text: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def test_llm_satisfies_protocol():
    client = OpenAiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: chat_response("x"))
    )
    assert isinstance(client, LlmClient)


def test_image_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return chat_response("読み取った文字")

    with OpenAiLlmClient(
        api_key=API_KEY, model="m", client=mock_client(handler)
    ) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png") == "読み取った文字"

    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["model"] == "m"
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


def test_charset_suffix_in_mime_type_is_tolerated():
    with OpenAiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: chat_response("ok"))
    ) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png; charset=binary")


def test_unsupported_image_type_is_rejected():
    with OpenAiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: chat_response("x"))
    ) as c:
        with pytest.raises(ProviderError):
            c.extract_text_from_image(PNG, mime_type="image/tiff")


def test_oversized_image_is_rejected():
    with OpenAiLlmClient(
        api_key=API_KEY, client=mock_client(lambda r: chat_response("x"))
    ) as c:
        with pytest.raises(ProviderError):
            c.extract_text_from_image(b"\x00" * (15 * 1024 * 1024), mime_type="image/png")


def test_no_choices_means_no_text():
    """文字が無い画像。取り込み全体を止めるほどのことではない。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with OpenAiLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png") == ""


def test_refusal_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"refusal": "できません"}}]}
        )

    with OpenAiLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        with pytest.raises(ProviderError, match="拒否"):
            c.extract_text_from_image(PNG, mime_type="image/png")
