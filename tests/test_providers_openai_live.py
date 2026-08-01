"""実際の OpenAI API に対する疎通テスト。

モックでは API 仕様との齟齬を検出できない。他のバックエンドでは実機テスト
でしか見つからない食い違いが実際に出ているため、ここも両方持つ。

KB_OPENAI_API_KEY が設定されているときだけ実行する。

    export KB_OPENAI_API_KEY="..."

**既定のモデル名をそのまま使う**。既定が実在しない、あるいはそのキーでは
使えないモデルを指していれば、ここで落ちて気づける（Gemini では、一覧に
出るのに新しいキーでは 404 になるモデルを実際に踏んでいる）。

画像は tests/data/ocr-sample.png（Gemini 側と同じもの）。
"""

import math
import os
from pathlib import Path

import pytest

from kb.providers.openai import OpenAiEmbeddingProvider, OpenAiLlmClient

pytestmark = pytest.mark.integration

IMAGE = Path(__file__).parent / "data" / "ocr-sample.png"


def cosine_distance(a, b) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b))


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("KB_OPENAI_API_KEY")
    if not key:
        pytest.skip("KB_OPENAI_API_KEY が未設定のため skip")

    # **残高不足は環境の問題であって、コードの不具合ではない。** 課金状態で
    # スイートが赤くなり続けるのは困るので skip に落とす。ただし理由は残す
    # ——「なぜか OpenAI のテストが走らない」と後から悩まないように。
    from kb.providers.base import ProviderError

    try:
        with OpenAiEmbeddingProvider(api_key=key, dimensions=64) as probe:
            probe.embed(["疎通確認"])
    except ProviderError as exc:
        if "残高" in str(exc):
            pytest.skip(f"OpenAI アカウントの残高不足のため skip: {exc}")
        raise
    return key


@pytest.fixture(scope="module")
def embedder(api_key):
    with OpenAiEmbeddingProvider(api_key=api_key) as provider:
        yield provider


@pytest.fixture(scope="module")
def llm(api_key):
    with OpenAiLlmClient(api_key=api_key) as client:
        yield client


def test_returns_requested_dimensions(embedder):
    vectors = embedder.embed(["ORA-01555 が発生し UNDO 表領域が不足している"])
    assert len(vectors) == 1
    assert len(vectors[0]) == embedder.dimensions
    assert math.isclose(math.sqrt(sum(v * v for v in vectors[0])), 1.0, rel_tol=1e-6)


def test_batch_preserves_order(embedder):
    """順序がずれると、別の知識のベクトルを保存してしまう。

    OpenAI は data の並びを入力順で返す保証がないため、index で並べ直して
    いる。その処理が実 API に対しても効いていることを確かめる。
    """
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
    """**開発用のハッシュ実装では確かめられない性質。**

    語彙を共有しない言い換えが近いベクトルになることを、実モデルで確認する。
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


def test_max_distance_separates_relevant_from_irrelevant(embedder):
    """**足切りの値そのものを実機で検証する。**

    max_distance はコードに書いた定数だが、根拠は API の返す距離の分布に
    ある。モデルが更新されて分布が動けば定数の方が黙って間違いになるので、
    「関連する質問は通り、無関係な質問は通らない」を実測で押さえる。

    ここが落ちたら定数を直すのではなく、まず分布を測り直すこと。
    """
    doc = (
        "DB接続エラー ORA-01555 の調査手順\n\n"
        "バッチ処理中に ORA-01555: snapshot too old が発生する場合、"
        "UNDO表領域の保持期間が不足している。UNDO_RETENTION を確認し、"
        "長時間実行されるクエリの実行時間より長く設定する。"
    )
    related = "バッチが途中で止まる。スナップショットが古すぎるというエラーが出ている"
    unrelated = "有給休暇の残日数はどこで確認できますか"

    dv, rv, uv = embedder.embed([doc, related, unrelated])
    near, far = cosine_distance(dv, rv), cosine_distance(dv, uv)
    print(f"\n関連: {near:.3f} / 無関係: {far:.3f} / 足切り: {embedder.max_distance}")

    assert near <= embedder.max_distance, "関連する質問が足切りされている"
    assert far > embedder.max_distance, (
        "無関係な質問が足切りを通り抜けている"
        "（この状態では「見つからないので新規登録」に到達できない）"
    )


def test_reads_text_from_image(llm):
    text = llm.extract_text_from_image(IMAGE.read_bytes(), mime_type="image/png")
    print(f"\n文字起こし結果:\n{text}")
    assert "ORA-01555" in text
    assert "UNDO" in text
    assert "データベース接続エラー" in text
