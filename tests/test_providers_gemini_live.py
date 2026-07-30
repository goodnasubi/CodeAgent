"""実際の Gemini API に対する疎通テスト。

モックでは API 仕様との齟齬を検出できない。他のバックエンドでは実機テスト
でしか見つからない食い違いが実際に出ているため、ここも両方持つ。

KB_GEMINI_API_KEY が設定されているときだけ実行する。

    export KB_GEMINI_API_KEY="..."

**既定のモデル名をそのまま使う**。既定が実在しないモデルを指していれば、
ここで落ちて気づける。

画像は tests/data/ocr-sample.png（同じディレクトリの .html を Chrome で
撮ったもの。この環境には画像ライブラリが無いため）:

    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" --headless \\
      --disable-gpu --screenshot="$(wslpath -w tests/data/ocr-sample.png)" \\
      --window-size=640,360 --hide-scrollbars \\
      "file:///$(wslpath -w tests/data/ocr-sample.html | tr '\\\\' '/')"
"""

import math
import os
from pathlib import Path

import pytest

from kb.providers.gemini import GeminiEmbeddingProvider, GeminiLlmClient

pytestmark = pytest.mark.integration

IMAGE = Path(__file__).parent / "data" / "ocr-sample.png"


def cosine_distance(a, b) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b))


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("KB_GEMINI_API_KEY")
    if not key:
        pytest.skip("KB_GEMINI_API_KEY が未設定のため skip")
    return key


@pytest.fixture(scope="module")
def embedder(api_key):
    with GeminiEmbeddingProvider(api_key=api_key) as provider:
        yield provider


@pytest.fixture(scope="module")
def llm(api_key):
    with GeminiLlmClient(api_key=api_key) as client:
        yield client


def test_returns_requested_dimensions(embedder):
    vectors = embedder.embed(["ORA-01555 が発生し UNDO 表領域が不足している"])
    assert len(vectors) == 1
    assert len(vectors[0]) == embedder.dimensions
    assert math.isclose(
        math.sqrt(sum(v * v for v in vectors[0])), 1.0, rel_tol=1e-6
    )


def test_batch_preserves_order(embedder):
    """順序がずれると、別の知識のベクトルを保存してしまう。"""
    texts = [
        "プリンターの用紙が詰まって印刷できない",
        "ORA-01555 が発生し UNDO 表領域が不足している",
        "社内 Wi-Fi に接続できない",
    ]
    batch = embedder.embed(texts)
    for i, text in enumerate(texts):
        single = embedder.embed([text])[0]
        assert cosine_distance(batch[i], single) < 0.05


def test_paraphrase_is_closer_than_unrelated(embedder):
    """**開発用のハッシュ実装では確かめられない性質**。

    語彙を共有しない言い換えが近いベクトルになることを、実モデルで確認する。
    max_distance の調整はこの距離を見て行う（ハッシュ実装の値で調整しない）。
    """
    base, paraphrase, unrelated = embedder.embed(
        [
            "ORA-01555 が発生し UNDO 表領域が不足している",
            "スナップショットが古すぎるというエラーで、ロールバック用の領域が足りない",
            "会議室のプロジェクターに接続できない",
        ]
    )
    near = cosine_distance(base, paraphrase)
    far = cosine_distance(base, unrelated)
    print(f"\n言い換え: {near:.3f} / 無関係: {far:.3f}")
    assert near < far


def test_reads_text_from_image(llm):
    text = llm.extract_text_from_image(IMAGE.read_bytes(), mime_type="image/png")
    print(f"\n文字起こし結果:\n{text}")
    assert "ORA-01555" in text
    assert "UNDO" in text
    assert "データベース接続エラー" in text
