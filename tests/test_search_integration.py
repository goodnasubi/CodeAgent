"""ハイブリッド検索の統合テスト。3 つの信号のまとまり方を確かめる。"""

import pytest

from kb.backends.base import Knowledge, KnowledgeBaseError, Relation
from kb.embeddings import HashingEmbeddingProvider
from kb.ingest import IngestPipeline
from kb.search import SIGNAL_GRAPH, SIGNAL_KEYWORD, SIGNAL_VECTOR, HybridSearch

pytestmark = pytest.mark.integration

# 「親」に ORA-01555 は一度も出てこない。語でも意味でも届かない知識。
DOCS = {
    "parent": ("DB接続エラー全般の調査手順", "接続設定と資格情報を確認し、表領域の状態を見る。"),
    "child": ("ORA-01555 が発生する", "UNDO 表領域が不足しているため拡張が必要。"),
    "printer": ("印刷できない", "プリンターの用紙が詰まっている。給紙トレイを確認する。"),
    "login": ("ログインできない", "会議室予約システムの認証設定を見直す。"),
}
QUERY = "ORA-01555"

# 取り込んだ知識と語彙がまったく重ならない問い合わせ。足切りの仕組みを
# 決定的に検証するために使う。
UNRELATED_QUERY = "QWERTYUIOP"


class FakeBackend:
    """KB の代わり。能力と応答を差し替えられるようにする。"""

    def __init__(self, *, keyword=True, relations=True, hits=(), fail=False):
        self.supports_keyword_search = keyword
        self.supports_relations = relations
        self._hits = list(hits)
        self._fail = fail
        self.search_calls = 0

    def search(self, query, *, limit=10):
        self.search_calls += 1
        if self._fail:
            raise KnowledgeBaseError("KB が落ちている")
        return [
            Knowledge(id=i, title=DOCS[i][0], body="", url="") for i in self._hits[:limit]
        ]


@pytest.fixture
def embedder():
    return HashingEmbeddingProvider(dimensions=256)


@pytest.fixture
def seeded(repo, tenant_id, embedder):
    pipeline = IngestPipeline(repository=repo, embedder=embedder)
    for issue_id, (title, body) in DOCS.items():
        pipeline.ingest_knowledge(
            tenant_id=tenant_id,
            knowledge=Knowledge(
                id=issue_id,
                title=title,
                body=body,
                url=f"https://kb.test/{issue_id}",
                labels=("bug",) if issue_id == "child" else (),
            ),
            relations=(
                [Relation(from_id="child", to_id="parent", kind="relates")]
                if issue_id == "child"
                else []
            ),
        )
    return pipeline


def run(repo, tenant_id, embedder, backend=None, *, candidates=1, **kwargs):
    """検索を実行する。

    文書が 4 件しかないため、候補数を絞らないと類似度検索が全件を返し、
    他の信号が何を足したのか見えなくなる。既定では候補 1 件に絞り、
    「類似度検索の上位に入らなかった知識」に他の信号が届くかを見る。
    """
    return HybridSearch(
        repository=repo, embedder=embedder, backend=backend, **kwargs
    ).search(
        tenant_id=tenant_id, query=QUERY, limit=5, candidates_per_signal=candidates
    )


# ------------------------------------------------------------ 基本のふるまい


def test_results_carry_title_and_url_without_calling_the_kb(
    repo, tenant_id, embedder, seeded
):
    """表示に必要な情報は取り込み時に控えてある。KB を叩かない。"""
    backend = FakeBackend(keyword=False, relations=False)
    response = run(repo, tenant_id, embedder, backend)

    top = response.results[0]
    assert top.kb_issue_id == "child"
    assert top.title == "ORA-01555 が発生する"
    assert top.url == "https://kb.test/child"
    assert top.labels == ("bug",)
    assert backend.search_calls == 0


def test_empty_query_returns_nothing(repo, tenant_id, embedder, seeded):
    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query="   "
    )
    assert response.results == []


def test_sources_show_why_each_hit_appeared(repo, tenant_id, embedder, seeded):
    backend = FakeBackend(hits=["printer"])
    response = run(repo, tenant_id, embedder, backend)
    by_id = {r.kb_issue_id: r for r in response.results}

    assert SIGNAL_VECTOR in by_id["child"].sources
    assert by_id["printer"].sources == (SIGNAL_KEYWORD,)
    assert by_id["parent"].sources == (SIGNAL_GRAPH,)


# ------------------------------------------------------- 信号の組み合わせ


def test_graph_reaches_knowledge_the_other_signals_cannot(
    repo, tenant_id, embedder, seeded
):
    """「親」は語も意味も一致しないが、人が張ったつながりで到達できる。"""
    without = run(repo, tenant_id, embedder, FakeBackend(relations=False))
    with_graph = run(repo, tenant_id, embedder, FakeBackend())

    assert "parent" not in [r.kb_issue_id for r in without.results]
    assert "parent" in [r.kb_issue_id for r in with_graph.results]


def test_direct_hit_still_outranks_graph_expansion(repo, tenant_id, embedder, seeded):
    response = run(repo, tenant_id, embedder, FakeBackend())
    assert response.results[0].kb_issue_id == "child"


def test_keyword_and_vector_agreement_boosts_a_hit(repo, tenant_id, embedder, seeded):
    """両方が拾ったものが上に来る。RRF の要点。"""
    only_vector = run(repo, tenant_id, embedder, FakeBackend(hits=["printer"]))
    both = run(repo, tenant_id, embedder, FakeBackend(hits=["child"]))

    child_rank_a = [r.kb_issue_id for r in only_vector.results].index("child")
    child_rank_b = [r.kb_issue_id for r in both.results].index("child")
    assert child_rank_b <= child_rank_a


# --------------------------------------------------- 能力差と縮退のふるまい


def test_backend_without_keyword_search_is_skipped_not_failed(
    repo, tenant_id, embedder, seeded
):
    """Re:lation のように検索を持たない KB でも成立する。"""
    backend = FakeBackend(keyword=False, relations=False)
    response = run(repo, tenant_id, embedder, backend)

    assert response.results, "類似度検索だけでも結果が出るべき"
    assert response.diagnostics.used == (SIGNAL_VECTOR,)
    assert SIGNAL_KEYWORD in response.diagnostics.skipped
    assert SIGNAL_GRAPH in response.diagnostics.skipped
    assert backend.search_calls == 0, "使えない検索を呼んでいる"


def test_kb_outage_degrades_instead_of_failing(repo, tenant_id, embedder, seeded):
    """KB が落ちても類似度検索は pgvector 側で返せる。"""
    response = run(repo, tenant_id, embedder, FakeBackend(fail=True))

    assert response.results
    assert SIGNAL_VECTOR in response.diagnostics.used
    assert "KB エラー" in response.diagnostics.skipped[SIGNAL_KEYWORD]


def test_no_backend_at_all_still_searches(repo, tenant_id, embedder, seeded):
    response = run(repo, tenant_id, embedder, None)
    assert response.results
    assert SIGNAL_KEYWORD in response.diagnostics.skipped


def test_keyword_search_with_no_match_says_so(repo, tenant_id, embedder, seeded):
    """0 件は「呼ばなかった」とは別物。診断で区別できないと切り分けられない。"""
    backend = FakeBackend(hits=[])
    response = run(repo, tenant_id, embedder, backend)

    assert backend.search_calls == 1, "呼んだ上での 0 件を確かめたい"
    assert SIGNAL_KEYWORD not in response.diagnostics.used
    assert response.diagnostics.skipped[SIGNAL_KEYWORD] == "一致する語が無い"


def test_knowledge_without_relations_reports_why_graph_was_skipped(
    repo, tenant_id, embedder, seeded
):
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="child", related=[])
    response = run(repo, tenant_id, embedder, FakeBackend())
    assert response.diagnostics.skipped[SIGNAL_GRAPH] == "つながりが無い"


def test_no_matches_returns_empty_without_crashing(repo, tenant_id, embedder):
    """まだ何も取り込んでいないテナント。"""
    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query="存在しない語"
    )
    assert response.results == []


# ------------------------------------------------------------------ 重み


def test_graph_weight_can_be_tuned(repo, tenant_id, embedder, seeded):
    strong = run(
        repo, tenant_id, embedder, FakeBackend(), weights={SIGNAL_GRAPH: 1.0}
    )
    weak = run(repo, tenant_id, embedder, FakeBackend(), weights={SIGNAL_GRAPH: 0.05})

    parent_strong = next(r for r in strong.results if r.kb_issue_id == "parent")
    parent_weak = next(r for r in weak.results if r.kb_issue_id == "parent")
    assert parent_weak.score < parent_strong.score


def test_ingest_knowledge_embeds_comments_too(repo, tenant_id, embedder):
    """combined_text 経由なので、コメントの内容でも検索に掛かる。"""
    pipeline = IngestPipeline(repository=repo, embedder=embedder)
    pipeline.ingest_knowledge(
        tenant_id=tenant_id,
        knowledge=Knowledge(
            id="with-comment",
            title="ネットワーク不調",
            body="詳細は調査中",
            url="",
            comments=("原因は MTU の設定ミスだった",),
        ),
    )
    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query="MTU 設定"
    )
    assert [r.kb_issue_id for r in response.results] == ["with-comment"]


# ------------------------------------------------ 関連なしを返さないこと


def test_unrelated_query_returns_nothing(repo, tenant_id, embedder, seeded):
    """足切りが無いと、どんな質問にも必ず何かが返ってしまう。

    無関係な結果が並ぶだけでなく、「見つからなかったので新しく登録する」
    という筋道に永久に到達できなくなる。

    検証には**語彙の重なりが皆無な**問い合わせを使う。開発用のハッシュ
    embedding は文字単位の衝突が多く、日本語の無関係な文でも距離が
    0.84 程度まで下がるため、閾値の「値」の妥当性はこれでは測れない
    （仕組みが働いていることだけを確かめる）。
    """
    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query=UNRELATED_QUERY
    )
    assert response.results == [], (
        f"無関係な質問に結果が返っている: "
        f"{[r.kb_issue_id for r in response.results]}"
    )


def test_related_query_still_returns_results(repo, tenant_id, embedder, seeded):
    """足切りを入れても、関連する知識は返ること。"""
    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query=QUERY
    )
    assert [r.kb_issue_id for r in response.results][0] == "child"


def test_threshold_can_be_disabled(repo, tenant_id, embedder, seeded):
    """距離の分布を調べたいときのために、足切りを外せること。"""
    response = HybridSearch(
        repository=repo, embedder=embedder, max_distance=None
    ).search(tenant_id=tenant_id, query=UNRELATED_QUERY)
    assert response.results, "足切りを外しても何も返らない"


def test_no_results_reports_why(repo, tenant_id, embedder, seeded):
    """空の結果に理由を添える。画面で「登録しますか」と促せるようにする。"""
    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query=UNRELATED_QUERY
    )
    assert response.diagnostics.skipped[SIGNAL_VECTOR] == "十分に近い知識が無い"
