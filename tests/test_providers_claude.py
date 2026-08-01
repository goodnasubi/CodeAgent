"""Claude プロバイダのモックテスト。

実 API との齟齬はこれでは検出できない（test_providers_claude_live.py が担当）。
ここで固定するのは、リクエストの組み立てと応答の解釈という自前のロジック。

SDK を使っているので、httpx の MockTransport を仕込んだクライアントを注入して
ネットワークに出さずに確かめる。
"""

import base64
import json

import anthropic
import httpx
import pytest

from kb.llm import LlmClient
from kb.providers import ProviderError
from kb.providers.claude import (
    MAX_INLINE_IMAGE_BYTES,
    ClaudeLlmClient,
)

API_KEY = "test-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def mock_client(handler) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=API_KEY,
        max_retries=0,  # 再試行されるとハンドラが複数回呼ばれて数え違える
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def message(blocks, *, stop_reason="end_turn", extra=None) -> httpx.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    body.update(extra or {})
    return httpx.Response(200, json=body)


def text_reply(text: str):
    return lambda request: message([{"type": "text", "text": text}])


def test_satisfies_protocol():
    client = ClaudeLlmClient(api_key=API_KEY, client=mock_client(text_reply("x")))
    assert isinstance(client, LlmClient)


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        ClaudeLlmClient(api_key="")


def test_image_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.content)
        return message([{"type": "text", "text": "読み取った文字"}])

    with ClaudeLlmClient(
        api_key=API_KEY, model="m", client=mock_client(handler)
    ) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png") == "読み取った文字"

    assert seen["url"].endswith("/v1/messages")
    assert seen["key"] == API_KEY
    assert seen["body"]["model"] == "m"
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "image"
    assert parts[0]["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(parts[0]["source"]["data"]) == PNG
    assert parts[1]["type"] == "text"


def test_thinking_is_left_to_the_model():
    """**thinking を送らない。** 指定の可否がモデルごとに違い、明示的な無効化が
    400 になるモデルがある。モデル既定に任せ、max_tokens で余裕を持たせる。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return message([{"type": "text", "text": "ok"}])

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        c.extract_text_from_image(PNG, mime_type="image/png")

    assert "thinking" not in seen["body"]
    assert seen["body"]["max_tokens"] >= 4096


def test_thinking_block_is_skipped():
    """**content[0] を決め打ちで読まない。** 熟考するモデルでは先頭が thinking
    ブロックになり、本文は後ろに来る（実測で sonnet-5 がそうなった）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return message(
            [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "本文"},
            ]
        )

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png") == "本文"


def test_multiple_text_blocks_are_joined():
    def handler(request: httpx.Request) -> httpx.Response:
        return message(
            [{"type": "text", "text": "前半"}, {"type": "text", "text": "後半"}]
        )

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png") == "前半後半"


def test_no_text_block_means_no_text():
    """文字が無い画像。取り込み全体を止めるほどのことではない。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return message([])

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png") == ""


def test_refusal_is_reported_before_reading_content():
    """**拒否は HTTP 200 で返る。** content を先に読むと空文字を成功として扱う。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return message(
            [],
            stop_reason="refusal",
            extra={"stop_details": {"type": "refusal", "category": "cyber"}},
        )

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        with pytest.raises(ProviderError, match="拒否"):
            c.extract_text_from_image(PNG, mime_type="image/png")


def test_refusal_without_stop_details_still_raises():
    """stop_details は無いこともある。分岐は stop_reason だけで行う。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return message([{"type": "text", "text": "途中まで"}], stop_reason="refusal")

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        with pytest.raises(ProviderError, match="拒否"):
            c.extract_text_from_image(PNG, mime_type="image/png")


def test_unsupported_image_type_is_rejected():
    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(text_reply("x"))) as c:
        with pytest.raises(ProviderError):
            c.extract_text_from_image(PNG, mime_type="image/tiff")


def test_charset_suffix_in_mime_type_is_tolerated():
    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(text_reply("ok"))) as c:
        assert c.extract_text_from_image(PNG, mime_type="image/png; charset=binary")


def test_oversized_image_is_rejected():
    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(text_reply("x"))) as c:
        with pytest.raises(ProviderError):
            c.extract_text_from_image(
                b"\x00" * (MAX_INLINE_IMAGE_BYTES + 1), mime_type="image/png"
            )


# ------------------------------------------------- 失敗の種類を取り違えない


@pytest.mark.parametrize(
    "status,pattern",
    [
        (401, "認証"),
        (404, "モデル"),
        (429, "レート制限"),
        (500, "エラーを返しました"),
    ],
)
def test_error_kinds_are_distinguished(status, pattern):
    """再試行できる失敗とできない失敗が同じ文言になると、運用で困る。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "boom"}})

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        with pytest.raises(ProviderError, match=pattern):
            c.extract_text_from_image(PNG, mime_type="image/png")


def test_connection_failure_becomes_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with ClaudeLlmClient(api_key=API_KEY, client=mock_client(handler)) as c:
        with pytest.raises(ProviderError, match="接続"):
            c.extract_text_from_image(PNG, mime_type="image/png")
