"""ポーリング取り込みの統合テスト。"""

from datetime import datetime, timedelta, timezone

import pytest

from kb.backends.base import Knowledge, KnowledgeBaseError, Relation
from kb.db.notifications import NotificationRepository
from kb.db.sync import SyncStateRepository
from kb.embeddings import HashingEmbeddingProvider
from kb.ingest import IngestPipeline
from kb.notifications import CHANNEL_IN_APP, InAppNotifier, NotificationDispatcher
from kb.search import HybridSearch
from kb.sync import EPOCH, SyncRunner

pytestmark = pytest.mark.integration


def knowledge(kid, *, title=None, body="本文", labels=()):
    return Knowledge(
        id=kid,
        title=title or f"知識 {kid}",
        body=body,
        url=f"https://kb.test/{kid}",
        labels=tuple(labels),
    )


class FakeBackend:
    def __init__(self, *, batch=(), relations=None, supports_relations=True, fail=False):
        self.supports_keyword_search = True
        self.supports_relations = supports_relations
        self.batch = list(batch)
        self._relations = relations or {}
        self._fail = fail
        self.since_calls = []
        self.relations_fail = False

    def updated_since(self, since):
        self.since_calls.append(since)
        if self._fail:
            raise KnowledgeBaseError("KB が落ちている")
        return iter(self.batch)

    def relations(self, knowledge_id):
        if self.relations_fail:
            raise KnowledgeBaseError("関連が取れない")
        return self._relations.get(knowledge_id, [])

    def search(self, query, *, limit=10):
        return []


@pytest.fixture
def embedder():
    return HashingEmbeddingProvider(dimensions=256)


@pytest.fixture
def state(repo):
    return SyncStateRepository(repo._conn)


@pytest.fixture
def notif_repo(repo):
    return NotificationRepository(repo._conn)


@pytest.fixture
def cleanup(repo, tenant_id):
    yield
    conn = repo._conn
    for table in (
        "sync_state",
        "knowledge_index",
        "notification_rules",
        "knowledge_label_state",
        "app_notifications",
    ):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))


def build(repo, embedder, state, backend, dispatcher=None):
    return SyncRunner(
        pipeline=IngestPipeline(repository=repo, embedder=embedder),
        backend=backend,
        state=state,
        dispatcher=dispatcher,
    )


# --------------------------------------------------------------- 基本の流れ


def test_first_run_starts_from_the_beginning(repo, tenant_id, embedder, state, cleanup):
    """初回は KB にある既存の知識をすべて取り込む。"""
    backend = FakeBackend(batch=[knowledge("a"), knowledge("b")])
    report = build(repo, embedder, state, backend).sync(tenant_id=tenant_id)

    assert backend.since_calls == [EPOCH]
    assert report.ingested == ["a", "b"]
    assert repo.count(tenant_id=tenant_id) == 2


def test_second_run_resumes_from_the_last_point(repo, tenant_id, embedder, state, cleanup):
    backend = FakeBackend(batch=[knowledge("a")])
    runner = build(repo, embedder, state, backend)
    runner.sync(tenant_id=tenant_id)

    backend.batch = [knowledge("b")]
    runner.sync(tenant_id=tenant_id)

    assert backend.since_calls[1] > EPOCH
    assert state.get(tenant_id=tenant_id).last_error is None


def test_start_point_is_taken_before_fetching(repo, tenant_id, embedder, state, cleanup):
    """取得中に更新された知識を取りこぼさないこと。

    取り終えた時刻を到達点にすると、取得中の更新が次回の範囲から外れる。
    """
    backend = FakeBackend(batch=[knowledge("a")])
    before = datetime.now(timezone.utc)
    build(repo, embedder, state, backend).sync(tenant_id=tenant_id)
    after = datetime.now(timezone.utc)

    synced = state.get(tenant_id=tenant_id).last_synced_at
    assert before <= synced <= after


def test_searchable_right_after_sync(repo, tenant_id, embedder, state, cleanup):
    """取り込んだ知識がそのまま検索に出ること。"""
    backend = FakeBackend(
        batch=[knowledge("db", title="ORA-01555 が発生する", body="UNDO 表領域が不足")]
    )
    build(repo, embedder, state, backend).sync(tenant_id=tenant_id)

    response = HybridSearch(repository=repo, embedder=embedder).search(
        tenant_id=tenant_id, query="ORA-01555"
    )
    assert [r.kb_issue_id for r in response.results] == ["db"]
    assert response.results[0].title == "ORA-01555 が発生する"


def test_relations_are_stored_during_sync(repo, tenant_id, embedder, state, cleanup):
    backend = FakeBackend(
        batch=[knowledge("child"), knowledge("parent")],
        relations={"child": [Relation(from_id="child", to_id="parent", kind="relates")]},
    )
    build(repo, embedder, state, backend).sync(tenant_id=tenant_id)

    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["parent"]) == {"child": 1}


def test_backend_without_relations_is_not_asked(repo, tenant_id, embedder, state, cleanup):
    """Re:lation のようにつながりを持たない KB では呼ばない。"""
    backend = FakeBackend(batch=[knowledge("a")], supports_relations=False)
    backend.relations_fail = True  # 呼ばれたら失敗するようにしておく

    report = build(repo, embedder, state, backend).sync(tenant_id=tenant_id)
    assert report.ok


# ------------------------------------------------------------ 失敗のふるまい


def test_fetch_failure_does_not_advance_the_checkpoint(
    repo, tenant_id, embedder, state, cleanup
):
    """取得できなければ到達点を進めない。次回もう一度同じ範囲を取りに行く。"""
    ok_backend = FakeBackend(batch=[knowledge("a")])
    runner = build(repo, embedder, state, ok_backend)
    runner.sync(tenant_id=tenant_id)
    checkpoint = state.get(tenant_id=tenant_id).last_synced_at

    broken = FakeBackend(fail=True)
    report = build(repo, embedder, state, broken).sync(tenant_id=tenant_id)

    assert report.aborted
    assert state.get(tenant_id=tenant_id).last_synced_at == checkpoint
    assert "落ちている" in state.get(tenant_id=tenant_id).last_error


def test_one_bad_item_does_not_stop_the_batch(repo, tenant_id, embedder, state, cleanup):
    backend = FakeBackend(batch=[knowledge("a"), knowledge("bad"), knowledge("c")])
    runner = build(repo, embedder, state, backend)

    original = runner._pipeline.ingest_knowledge

    def fail_on_bad(*, tenant_id, knowledge, relations=()):
        if knowledge.id == "bad":
            raise RuntimeError("この知識は壊れている")
        return original(tenant_id=tenant_id, knowledge=knowledge, relations=relations)

    runner._pipeline.ingest_knowledge = fail_on_bad
    report = runner.sync(tenant_id=tenant_id)

    assert report.ingested == ["a", "c"], "1 件の失敗で残りが止まっている"
    assert report.failed == [("bad", "この知識は壊れている")]


def test_partial_failure_keeps_the_checkpoint_for_a_retry(
    repo, tenant_id, embedder, state, cleanup
):
    backend = FakeBackend(batch=[knowledge("a")])
    runner = build(repo, embedder, state, backend)
    runner._pipeline.ingest_knowledge = lambda **kw: (_ for _ in ()).throw(
        RuntimeError("失敗")
    )
    runner.sync(tenant_id=tenant_id)

    assert state.get(tenant_id=tenant_id).last_synced_at is None
    assert "1 件" in state.get(tenant_id=tenant_id).last_error


def test_relations_failure_still_ingests_the_knowledge(
    repo, tenant_id, embedder, state, cleanup
):
    """つながりが取れなくても知識そのものは失わない。"""
    backend = FakeBackend(batch=[knowledge("a")])
    backend.relations_fail = True

    report = build(repo, embedder, state, backend).sync(tenant_id=tenant_id)
    assert report.ingested == ["a"]
    assert repo.count(tenant_id=tenant_id) == 1


# ------------------------------------------------------------- 通知との連携


def test_label_added_between_polls_notifies(
    repo, tenant_id, embedder, state, notif_repo, cleanup
):
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher = NotificationDispatcher(
        repository=notif_repo, notifiers=[InAppNotifier(notif_repo)]
    )
    backend = FakeBackend(batch=[knowledge("a")])
    runner = build(repo, embedder, state, backend, dispatcher)

    runner.sync(tenant_id=tenant_id)  # 初回取り込み
    assert notif_repo.unread(tenant_id=tenant_id) == []

    backend.batch = [knowledge("a", labels=["重大"])]
    report = runner.sync(tenant_id=tenant_id)

    assert report.notified == ["a"]
    inbox = notif_repo.unread(tenant_id=tenant_id)
    assert len(inbox) == 1 and inbox[0].label == "重大"


def test_first_import_does_not_flood_notifications(
    repo, tenant_id, embedder, state, notif_repo, cleanup
):
    """既存知識にラベルが付いていても、初回取り込みでは通知しない。"""
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher = NotificationDispatcher(
        repository=notif_repo, notifiers=[InAppNotifier(notif_repo)]
    )
    backend = FakeBackend(
        batch=[knowledge(str(i), labels=["重大"]) for i in range(20)]
    )
    report = build(repo, embedder, state, backend, dispatcher).sync(tenant_id=tenant_id)

    assert len(report.ingested) == 20
    assert notif_repo.unread(tenant_id=tenant_id) == [], "運用開始時に通知が溢れている"


def test_explicit_since_overrides_the_checkpoint(
    repo, tenant_id, embedder, state, cleanup
):
    """取り直しのために起点を指定できること。"""
    backend = FakeBackend(batch=[knowledge("a")])
    runner = build(repo, embedder, state, backend)
    runner.sync(tenant_id=tenant_id)

    wanted = datetime.now(timezone.utc) - timedelta(days=7)
    runner.sync(tenant_id=tenant_id, since=wanted)
    assert backend.since_calls[-1] == wanted


def test_empty_batch_still_advances_the_checkpoint(
    repo, tenant_id, embedder, state, cleanup
):
    """更新が無かった回でも到達点は進める（毎回全件を見に行かないため）。"""
    backend = FakeBackend(batch=[])
    build(repo, embedder, state, backend).sync(tenant_id=tenant_id)
    assert state.get(tenant_id=tenant_id).last_synced_at is not None
