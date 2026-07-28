"""取り込み → 検索の統合テスト。

実際の PostgreSQL + pgvector に対して実行する。KB_TEST_DSN が未設定なら skip。
"""

import pytest

from kb.embeddings import HashingEmbeddingProvider
from kb.ingest import IngestPipeline

pytestmark = pytest.mark.integration


DOC_A = "ORA-01555 が発生する。UNDO 表領域が不足しているため拡張が必要。"
DOC_B = "プリンターの用紙が詰まり印刷できない。給紙トレイを確認する。"
DOC_C = "ネットワーク共有フォルダに接続できない。DNS の設定を見直す。"
DOC_D = "UNDO 表領域の不足により ORA-01555 のエラーが出ている。"


@pytest.fixture
def pipeline(repo):
    return IngestPipeline(repository=repo, embedder=HashingEmbeddingProvider(dimensions=768))


def _ingest_all(pipeline, tenant_id):
    for issue_id, text in [("issue-A", DOC_A), ("issue-B", DOC_B), ("issue-C", DOC_C)]:
        pipeline.ingest_text(
            tenant_id=tenant_id,
            kb_issue_id=issue_id,
            text=text,
            source_name=f"{issue_id}.txt",
        )


def test_ingest_stores_chunks(pipeline, repo, tenant_id):
    result = pipeline.ingest_text(
        tenant_id=tenant_id, kb_issue_id="issue-A", text=DOC_A, source_name="A.txt"
    )
    assert result.chunks == 1
    assert result.dimensions == 768
    assert repo.count(tenant_id=tenant_id) == 1


def test_finds_the_similar_document(pipeline, repo, tenant_id):
    """A/B/C を登録し、D で検索すると A が最も近い。"""
    _ingest_all(pipeline, tenant_id)

    embedder = HashingEmbeddingProvider(dimensions=768)
    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=embedder.embed([DOC_D])[0],
        model=embedder.model,
        limit=3,
    )

    assert hits, "検索結果が空になっている"
    assert hits[0].kb_issue_id == "issue-A"
    assert hits[0].distance < 1.0


def test_reingest_replaces_chunks_instead_of_duplicating(pipeline, repo, tenant_id):
    """KB 側で編集された知識を取り込み直しても、行が二重にならない。"""
    pipeline.ingest_text(tenant_id=tenant_id, kb_issue_id="issue-A", text=DOC_A)
    first = repo.count(tenant_id=tenant_id)

    pipeline.ingest_text(tenant_id=tenant_id, kb_issue_id="issue-A", text=DOC_A + " 追記。")
    assert repo.count(tenant_id=tenant_id) == first


def test_long_document_is_chunked_but_collapses_to_one_hit(pipeline, repo, tenant_id):
    """長い文書は複数行になるが、検索結果では 1 件に畳まれる。"""
    long_text = "\n\n".join(f"手順{i}: UNDO 表領域を確認する。" * 20 for i in range(12))
    result = pipeline.ingest_text(
        tenant_id=tenant_id, kb_issue_id="issue-long", text=long_text
    )
    assert result.chunks > 1

    embedder = HashingEmbeddingProvider(dimensions=768)
    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=embedder.embed(["UNDO 表領域の確認手順"])[0],
        model=embedder.model,
        limit=10,
    )
    assert [h.kb_issue_id for h in hits].count("issue-long") == 1


def test_other_tenants_are_not_visible(pipeline, repo, tenant_id):
    """テナント分離。パーティションを跨いで結果が漏れない。"""
    import uuid

    other = uuid.uuid4()
    repo.ensure_tenant(other)
    try:
        _ingest_all(pipeline, tenant_id)
        embedder = HashingEmbeddingProvider(dimensions=768)
        hits = repo.search(
            tenant_id=other,
            query_embedding=embedder.embed([DOC_D])[0],
            model=embedder.model,
        )
        assert hits == []
    finally:
        repo._conn.execute(f'DROP TABLE IF EXISTS "kc_{other.hex}"')


def test_different_models_do_not_mix(pipeline, repo, tenant_id):
    """モデル切り替え中は新旧が併存し、検索は指定モデルの行だけを見る。"""
    _ingest_all(pipeline, tenant_id)

    new_model = HashingEmbeddingProvider(dimensions=1536, model="hashing-dev-v2")
    IngestPipeline(repository=repo, embedder=new_model).ingest_text(
        tenant_id=tenant_id, kb_issue_id="issue-A", text=DOC_A
    )

    assert repo.count(tenant_id=tenant_id, model="hashing-dev") == 3
    assert repo.count(tenant_id=tenant_id, model="hashing-dev-v2") == 1

    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=new_model.embed([DOC_D])[0],
        model=new_model.model,
        limit=5,
    )
    assert [h.kb_issue_id for h in hits] == ["issue-A"]


def test_ef_search_is_applied_when_index_is_used(repo, tenant_id, conn):
    """ef_search が実際に適用されていることの回帰テスト。

    ef_search は再現率だけでなく「返せる行数の上限」でもある。既定の 40 の
    ままだと overfetch をいくつにしても 40 件しか返らない。

    この不具合は HNSW を経由したときにだけ表面化する。行数が少ないうちは
    プランナが Sort を選び、その経路では ef_search が関係しないため、
    インデックスが実際に使われる行数まで積んだ上で確認する。
    次元数は挙動に影響しないので、テスト時間を抑えるため小さくしている。
    """
    import random

    dim = 128
    model = "efsearch-fixture"
    # 行数が少ないとプランナが HNSW を選ばず、この不具合が表面化しない。
    # 2000 行では選ばれたり選ばれなかったりするため、確実に選ばれる 4000 行にする。
    issues = 4000
    repo.ensure_dimension_index(tenant_id, dim)

    # ベクトルは散らす。互いにほぼ同一のベクトルだと ef_search の上限が
    # 現れないことがあり、その場合このテストは不具合を検出できない。
    rng = random.Random(20260728)

    def dispersed() -> str:
        v = [0.0] * dim
        for _ in range(8):
            v[rng.randrange(dim)] += rng.random()
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return "[" + ",".join(repr(x / norm) for x in v) + "]"

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO knowledge_chunks (tenant_id, kb_issue_id, chunk_index,"
            " content, embedding, embedding_model, embedding_dim)"
            " VALUES (%s, %s, 0, %s, %s, %s, %s)",
            [
                (tenant_id, f"issue-{i:05d}", f"記録 {i}", dispersed(), model, dim)
                for i in range(issues)
            ],
        )
    conn.execute(f'ANALYZE "kc_{tenant_id.hex}"')

    query = [0.0] * dim
    query[0] = 1.0

    # search() が実際に発行するクエリと同じ形で確認する。embedding_model の
    # 絞り込みを省くと別のプランになりうるため、条件を揃えること。
    # また「Index Scan」だけで判定すると Bitmap Index Scan（主キー経由）にも
    # 一致してしまうため、インデックス名まで見る。
    plan = conn.execute(
        f"EXPLAIN SELECT kb_issue_id FROM knowledge_chunks"
        f" WHERE tenant_id = %s AND embedding_dim = {dim} AND embedding_model = %s"
        f" ORDER BY embedding::vector({dim}) <=> %s::vector({dim}) LIMIT 100",
        (tenant_id, model, "[" + ",".join(repr(v) for v in query) + "]"),
    ).fetchall()
    assert any("Index Scan using idx_" in row[0] for row in plan), (
        f"HNSW が使われておらず ef_search の検証になっていない: {plan}"
    )

    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=query,
        model=model,
        limit=50,
        overfetch=100,
    )
    assert len(hits) == 50, f"ef_search が効いていない（{len(hits)} 件しか返らない）"


def test_overfetch_smaller_than_limit_is_rejected(repo, tenant_id):
    with pytest.raises(ValueError):
        repo.search(
            tenant_id=tenant_id,
            query_embedding=[0.0] * 768,
            model="hashing-dev",
            limit=50,
            overfetch=10,
        )
