"""つながりを検索に効かせる部分の統合テスト。

類似度検索・キーワード検索・グラフ展開の 3 本を RRF でマージする。
"""

import pytest

from kb.embeddings import HashingEmbeddingProvider
from kb.ingest import IngestPipeline
from kb.ranking import rank_by_hops, reciprocal_rank_fusion

pytestmark = pytest.mark.integration

# 「親」には ORA-01555 という語が一度も出てこない。語でも意味でも
# 類似度検索・キーワード検索のどちらからも届かない知識にする。
PARENT = "DB接続エラー全般の調査手順。まず接続設定と資格情報を確認し、次に表領域の状態を見る。"
CHILD = "ORA-01555 が発生する。UNDO 表領域が不足しているため拡張が必要。"
UNRELATED_A = "プリンターの用紙が詰まり印刷できない。給紙トレイを確認する。"
UNRELATED_B = "会議室の予約システムにログインできない。認証の設定を見直す。"

# 検索語。HashingEmbeddingProvider は bag-of-words ハッシュなので、助詞など
# 一般的な文字を混ぜると衝突で順位が乱れる。固有語だけで問い合わせる。
QUERY = "ORA-01555"


@pytest.fixture
def seeded(repo, tenant_id):
    embedder = HashingEmbeddingProvider(dimensions=256)
    pipeline = IngestPipeline(repository=repo, embedder=embedder)
    for issue_id, text in [
        ("parent", PARENT),
        ("child", CHILD),
        ("other-a", UNRELATED_A),
        ("other-b", UNRELATED_B),
    ]:
        pipeline.ingest_text(tenant_id=tenant_id, kb_issue_id=issue_id, text=text)

    # 子が親を参照している（KB 側で人が張ったつながり）
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="child", related=[("parent", "references")]
    )
    return embedder


def test_similarity_alone_misses_the_related_knowledge(repo, tenant_id, seeded):
    """前提の確認。類似度検索だけでは親に届かない。"""
    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=seeded.embed([QUERY])[0],
        model=seeded.model,
        limit=2,
    )
    assert [h.kb_issue_id for h in hits][0] == "child"
    assert "parent" not in [h.kb_issue_id for h in hits]


def test_graph_expansion_surfaces_the_related_knowledge(repo, tenant_id, seeded):
    """つながりを辿ると、語も意味も一致しない親に到達できる。"""
    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=seeded.embed([QUERY])[0],
        model=seeded.model,
        limit=2,
    )
    top = [h.kb_issue_id for h in hits]

    neighbours = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=top, hops=1)
    assert "parent" in neighbours, "つながりを辿れていない"

    fused = reciprocal_rank_fusion(
        {"vector": top, "graph": rank_by_hops(neighbours)},
        weights={"graph": 0.5},
    )
    ranked = [h.kb_issue_id for h in fused]
    assert ranked[0] == "child", "直接ヒットが上位から外れている"
    assert "parent" in ranked
    assert ranked.index("parent") < ranked.index("other-a") if "other-a" in ranked else True


def test_fused_result_records_why_each_hit_appeared(repo, tenant_id, seeded):
    """「なぜ出てきたか」を UI に出せること。"""
    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=seeded.embed([QUERY])[0],
        model=seeded.model,
        limit=2,
    )
    top = [h.kb_issue_id for h in hits]
    neighbours = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=top, hops=1)

    fused = reciprocal_rank_fusion(
        {"vector": top, "graph": rank_by_hops(neighbours)}
    )
    by_id = {h.kb_issue_id: h for h in fused}
    assert by_id["child"].sources == ("vector",)
    assert by_id["parent"].sources == ("graph",)


def test_graph_weight_controls_how_strongly_relations_matter(repo, tenant_id, seeded):
    """重みでグラフ展開の効き具合を調整できること。"""
    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=seeded.embed([QUERY])[0],
        model=seeded.model,
        limit=3,
    )
    top = [h.kb_issue_id for h in hits]
    neighbours = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=top, hops=1)
    lists = {"vector": top, "graph": rank_by_hops(neighbours)}

    strong = {h.kb_issue_id: h.score for h in reciprocal_rank_fusion(lists)}
    weak = {
        h.kb_issue_id: h.score
        for h in reciprocal_rank_fusion(lists, weights={"graph": 0.1})
    }
    assert weak["parent"] < strong["parent"]


def test_no_relations_means_graph_adds_nothing(repo, tenant_id, seeded):
    """つながりが無い知識では、グラフ展開は結果を変えない。"""
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="child", related=[])

    hits = repo.search(
        tenant_id=tenant_id,
        query_embedding=seeded.embed([QUERY])[0],
        model=seeded.model,
        limit=2,
    )
    top = [h.kb_issue_id for h in hits]
    neighbours = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=top, hops=1)
    assert neighbours == {}

    fused = reciprocal_rank_fusion({"vector": top, "graph": rank_by_hops(neighbours)})
    assert [h.kb_issue_id for h in fused] == top
