import math

import pytest

from kb.embeddings import EmbeddingProvider, HashingEmbeddingProvider


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_satisfies_provider_protocol():
    assert isinstance(HashingEmbeddingProvider(), EmbeddingProvider)


def test_dimensions_and_model_are_reported():
    p = HashingEmbeddingProvider(dimensions=256, model="test-model")
    assert p.dimensions == 256
    assert p.model == "test-model"
    assert len(p.embed(["テスト"])[0]) == 256


def test_output_length_matches_input():
    p = HashingEmbeddingProvider()
    assert len(p.embed(["a", "b", "c"])) == 3
    assert p.embed([]) == []


def test_vectors_are_normalized():
    p = HashingEmbeddingProvider()
    for vec in p.embed(["ORA-01555 が発生", "ログインできない"]):
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


def test_deterministic():
    a = HashingEmbeddingProvider().embed(["同じ入力"])[0]
    b = HashingEmbeddingProvider().embed(["同じ入力"])[0]
    assert a == b


def test_shared_vocabulary_is_closer_than_unrelated():
    """E2E テストが意味を持つ前提の確認。

    語彙を共有するテキストが近いベクトルになる。この性質があるため、
    「似た知識が見つかること」を API キーなしで検証できる。
    """
    p = HashingEmbeddingProvider()
    base, similar, unrelated = p.embed(
        [
            "ORA-01555 が発生し UNDO 表領域が不足",
            "UNDO 表領域の不足で ORA-01555 が出る",
            "プリンターの用紙が詰まって印刷できない",
        ]
    )
    assert cosine(base, similar) > cosine(base, unrelated)


def test_empty_text_produces_usable_vector():
    """ゼロベクトルはコサイン距離が定義できないため、単位ベクトルを返す。"""
    vec = HashingEmbeddingProvider().embed([""])[0]
    assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


def test_invalid_dimensions_rejected():
    with pytest.raises(ValueError):
        HashingEmbeddingProvider(dimensions=0)


# --------------------------------------------- 足切り距離はプロバイダが持つ


def test_provider_declares_its_own_max_distance():
    """モデルごとに分布が違うので、共通の定数ではなく provider が申告する。"""
    assert HashingEmbeddingProvider().max_distance == 0.85
    assert HashingEmbeddingProvider(max_distance=0.4).max_distance == 0.4


def test_hashing_and_gemini_do_not_share_a_threshold():
    """**この 2 つが同じ値になったら、どちらかが間違っている。**

    ハッシュ実装は単語の重なりしか見ないため距離が全体的に大きい。
    実モデルの値を流用すると足切りが効かず、新規登録の導線が消える。
    """
    from kb.providers.gemini import DEFAULT_MAX_DISTANCE as GEMINI_MAX

    assert GEMINI_MAX < HashingEmbeddingProvider().max_distance


def test_hybrid_search_takes_the_threshold_from_the_embedder():
    from kb.search import HybridSearch

    embedder = HashingEmbeddingProvider(max_distance=0.33)
    # repository は __init__ では触られない
    assert HybridSearch(repository=None, embedder=embedder)._max_distance == 0.33


def test_explicit_threshold_overrides_the_embedder():
    from kb.search import HybridSearch

    embedder = HashingEmbeddingProvider(max_distance=0.33)
    search = HybridSearch(repository=None, embedder=embedder, max_distance=0.7)
    assert search._max_distance == 0.7


def test_none_still_means_no_cutoff():
    """None は「provider に訊く」ではなく「足切りしない」。区別が要る。"""
    from kb.search import HybridSearch

    embedder = HashingEmbeddingProvider(max_distance=0.33)
    search = HybridSearch(repository=None, embedder=embedder, max_distance=None)
    assert search._max_distance is None
