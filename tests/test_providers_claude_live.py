"""実際の Anthropic API に対する疎通テスト。

モックでは API 仕様との齟齬を検出できない。他のプロバイダでは実機テストで
しか見つからない食い違いが実際に出ているため、ここも両方持つ。

KB_ANTHROPIC_API_KEY が設定されているときだけ実行する。

    export KB_ANTHROPIC_API_KEY="..."

**既定のモデル名をそのまま使う**。既定が実在しない、あるいはそのキーでは
使えないモデルを指していれば、ここで落ちて気づける（Gemini では、一覧に
出るのに新しいキーでは 404 になるモデルを実際に踏んでいる）。

embedding のテストは無い。**Anthropic は embedding の API を提供していない。**
"""

import os
from pathlib import Path

import pytest

from kb.providers.claude import ClaudeLlmClient

pytestmark = pytest.mark.integration

IMAGE = Path(__file__).parent / "data" / "ocr-sample.png"


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("KB_ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("KB_ANTHROPIC_API_KEY が未設定のため skip")
    return key


@pytest.fixture(scope="module")
def llm(api_key):
    with ClaudeLlmClient(api_key=api_key) as client:
        yield client


def test_reads_text_from_image(llm):
    text = llm.extract_text_from_image(IMAGE.read_bytes(), mime_type="image/png")
    print(f"\nモデル: {llm.model}\n文字起こし結果:\n{text}")
    assert "ORA-01555" in text
    assert "UNDO" in text
    assert "データベース接続エラー" in text


def test_transcribes_rather_than_describes(llm):
    """**説明文を書かせない。** プロンプトの効きを実機で確かめる。

    markitdown の既定プロンプトは画像の説明を求めるもので、用途が違う。
    「この画像は〜を示しています」のような前置きが混ざると、そのまま
    知識として保存されてしまう。
    """
    text = llm.extract_text_from_image(IMAGE.read_bytes(), mime_type="image/png")
    for phrase in ("この画像", "画像には", "示しています", "と思われます"):
        assert phrase not in text, f"説明文が混ざっている: {phrase!r}"
