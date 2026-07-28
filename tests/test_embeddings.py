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
