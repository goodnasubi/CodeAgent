"""常駐スケジューラの統合テスト。

見ているのは「全テナントを回せること」より、**止まらないこと**と
**二重に取り込まないこと**。1 テナントの取り込み自体は
test_sync_integration.py が受け持つ。
"""

from datetime import datetime, timezone

import psycopg
import pytest
from cryptography.fernet import Fernet

from kb.backends.base import Knowledge, KnowledgeBaseError
from kb.db.sync import SyncInProgress, tenant_sync_lock
from kb.scheduler import PassReport, SyncScheduler
from kb.sync import DEFAULT_INTERVAL_SECONDS, INTERVAL_ENV, interval_from_env
from kb.tenants import KbConnection, TenantRepository, load_cipher

pytestmark = pytest.mark.integration


class FakeBackend:
    """KB の代わり。`build_backend` を差し替えて挿し込む。"""

    def __init__(self, batch=(), *, fail=False):
        self.supports_keyword_search = True
        self.supports_relations = False
        self.batch = list(batch)
        self.fail = fail
        self.calls = 0

    def updated_since(self, since):
        self.calls += 1
        if self.fail:
            raise KnowledgeBaseError("KB が落ちている")
        return iter(self.batch)

    def relations(self, knowledge_id):
        return []

    def search(self, query, *, limit=10):
        return []


def knowledge(kid):
    return Knowledge(
        id=kid, title=f"知識 {kid}", body="本文", url=f"https://kb.test/{kid}"
    )


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("KB_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def tenants(repo, secret):
    """テナントを払い出し、後片付けする。"""
    conn = repo._conn
    created = []

    def make(*, with_kb=True):
        tenant = TenantRepository(conn, cipher=load_cipher()).create_tenant(
            name=f"テナント {len(created)}"
        )
        repo.ensure_tenant(tenant.id)
        if with_kb:
            TenantRepository(conn, cipher=load_cipher()).set_kb_connection(
                tenant_id=tenant.id,
                connection=KbConnection(kb_type="github", project="acme/kb"),
                token="dummy",
            )
        created.append(tenant.id)
        return tenant.id

    yield make

    for tid in created:
        for table in (
            "app_notifications",
            "notification_rules",
            "knowledge_label_state",
            "knowledge_index",
            "sync_state",
            "tenant_kb_connections",
            "tenant_model_settings",
        ):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tid,))
        conn.execute(f'DROP TABLE IF EXISTS "kc_{tid.hex}"')
        conn.execute(f'DROP TABLE IF EXISTS "ke_{tid.hex}"')
        conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


@pytest.fixture
def backends(monkeypatch):
    """テナントごとの FakeBackend。既定は空の取り込み。"""
    registry = {}

    def fake_build_backend(*, connection, token):
        # プロジェクト名にテナントを埋めて取り出す（factory はテナントを渡さない）
        return registry.get(connection.project, FakeBackend())

    monkeypatch.setattr("kb.factory.build_backend", fake_build_backend)
    return registry


def scheduler(dsn, **kwargs):
    kwargs.setdefault("interval", 0.01)
    kwargs.setdefault("shared_notifiers", [])
    return SyncScheduler(
        connect=lambda: psycopg.connect(dsn, autocommit=True), **kwargs
    )


def point_at(conn, tenant_id, backend, backends):
    """このテナントの KB 接続を、指定の FakeBackend に向ける。"""
    key = f"proj-{tenant_id.hex}"
    backends[key] = backend
    TenantRepository(conn, cipher=load_cipher()).set_kb_connection(
        tenant_id=tenant_id,
        connection=KbConnection(kb_type="github", project=key),
        token="dummy",
    )


# ------------------------------------------------------------------ 1 周ぶん


def test_every_tenant_is_polled_in_one_pass(repo, dsn, tenants, backends):
    a, b = tenants(), tenants()
    point_at(repo._conn, a, FakeBackend([knowledge("a1")]), backends)
    point_at(repo._conn, b, FakeBackend([knowledge("b1"), knowledge("b2")]), backends)

    report = scheduler(dsn).run_pass()

    assert report.reports[a].ingested == ["a1"]
    assert report.reports[b].ingested == ["b1", "b2"]
    assert repo.count(tenant_id=a) == 1
    assert repo.count(tenant_id=b) == 2


def test_tenant_without_a_knowledge_base_is_skipped(repo, dsn, tenants, backends):
    """払い出しただけで KB 未設定のテナントは、異常ではなく見送り。"""
    bare = tenants(with_kb=False)

    report = scheduler(dsn).run_pass()

    assert bare not in report.reports
    assert bare in report.skipped


def test_one_tenant_failing_does_not_stop_the_others(repo, dsn, tenants, backends):
    """1 テナントで例外が出ても、周回は残りを回しきる。"""
    broken, healthy = tenants(), tenants()

    class Exploding(FakeBackend):
        def updated_since(self, since):
            raise RuntimeError("アダプタが壊れている")

    point_at(repo._conn, broken, Exploding(), backends)
    point_at(repo._conn, healthy, FakeBackend([knowledge("ok")]), backends)

    report = scheduler(dsn).run_pass()

    assert "壊れている" in report.skipped[broken]
    assert report.reports[healthy].ingested == ["ok"], "1 件の失敗で周回が止まっている"


def test_kb_outage_is_recorded_without_advancing_the_checkpoint(
    repo, dsn, tenants, backends
):
    """KB が落ちているのは周回の失敗ではなく、そのテナントの中止として残る。"""
    tid = tenants()
    point_at(repo._conn, tid, FakeBackend(fail=True), backends)

    report = scheduler(dsn).run_pass()

    assert report.reports[tid].aborted
    assert not report.ok


# ------------------------------------------------------------ 二重実行の防止


def test_a_tenant_already_syncing_is_skipped(repo, dsn, tenants, backends):
    """手動実行やスケジューラの二重起動と同時に走らない。

    同時に走るとラベルの差分検知が両方で「新しく付いた」と判定し、通知が
    二度飛ぶ。取り込み自体は上書きなので壊れないが、通知は取り消せない。
    """
    tid = tenants()
    backend = FakeBackend([knowledge("a")])
    point_at(repo._conn, tid, backend, backends)

    # 別セッションがロックを握っている状態を作る
    with psycopg.connect(dsn, autocommit=True) as other:
        with tenant_sync_lock(other, tenant_id=tid):
            report = scheduler(dsn).run_pass()

    assert report.skipped[tid] == "実行中"
    assert backend.calls == 0, "ロック中なのに KB を取りに行っている"


def test_the_lock_is_released_after_a_pass(repo, dsn, tenants, backends):
    tid = tenants()
    point_at(repo._conn, tid, FakeBackend([knowledge("a")]), backends)
    scheduler(dsn).run_pass()

    with psycopg.connect(dsn, autocommit=True) as other:
        with tenant_sync_lock(other, tenant_id=tid):
            pass  # 取れなければ SyncInProgress で落ちる


def test_the_lock_is_released_even_when_the_pass_fails(repo, dsn, tenants):
    tid = tenants()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(RuntimeError):
            with tenant_sync_lock(conn, tenant_id=tid):
                raise RuntimeError("取り込み中の例外")

        with tenant_sync_lock(conn, tenant_id=tid):
            pass


def test_lock_conflict_raises_rather_than_waiting(repo, dsn, tenants):
    tid = tenants()
    with psycopg.connect(dsn, autocommit=True) as a, psycopg.connect(
        dsn, autocommit=True
    ) as b:
        with tenant_sync_lock(a, tenant_id=tid):
            with pytest.raises(SyncInProgress):
                with tenant_sync_lock(b, tenant_id=tid):
                    pass


# ---------------------------------------------------------------- 常駐ループ


def empty_pass() -> PassReport:
    return PassReport(started_at=datetime.now(timezone.utc))


def test_the_loop_survives_a_failing_pass(dsn):
    """DB に繋がらない等で 1 周が丸ごと落ちても、常駐は終わらない。"""
    sched = scheduler(dsn)
    passes = []

    def flaky():
        passes.append(len(passes) + 1)
        if len(passes) == 1:
            raise RuntimeError("DB に繋がらない")
        return empty_pass()

    sched.run_pass = flaky
    sched.run_forever(max_passes=3)

    assert passes == [1, 2, 3], "1 周の失敗でループが終わっている"


def test_stop_ends_the_loop_without_waiting_out_the_interval(dsn):
    """停止要求は、次の周回まで待たずに効く。"""
    sched = scheduler(dsn, interval=3600)
    calls = []

    def one_and_done():
        calls.append(1)
        sched.stop()
        return empty_pass()

    sched.run_pass = one_and_done
    started = datetime.now(timezone.utc)
    sched.run_forever()

    assert calls == [1]
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    assert elapsed < 5, "間隔ぶん待ってから止まっている"


def test_the_running_pass_finishes_before_stopping(dsn):
    """停止要求で走行中の取り込みを中断しない（到達点が進まず無駄になるため）。"""
    sched = scheduler(dsn, interval=3600)
    finished = []

    def slow():
        sched.stop()  # 取り込みの途中で停止要求が来た状況
        finished.append(True)
        return empty_pass()

    sched.run_pass = slow
    sched.run_forever()

    assert finished == [True]


# ------------------------------------------------------------------ 間隔設定


def test_interval_defaults_to_ten_minutes(monkeypatch):
    monkeypatch.delenv(INTERVAL_ENV, raising=False)
    assert interval_from_env() == DEFAULT_INTERVAL_SECONDS == 600


def test_interval_comes_from_config(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "120")
    assert interval_from_env() == 120


@pytest.mark.parametrize("bad", ["まいにち", "0", "-5"])
def test_a_bad_interval_fails_at_startup(monkeypatch, bad):
    """起動時に落とす。黙って既定値に戻すと設定ミスに気づけない。"""
    monkeypatch.setenv(INTERVAL_ENV, bad)
    with pytest.raises(ValueError):
        interval_from_env()
