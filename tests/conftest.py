import os
import uuid

import pytest

DSN_ENV = "KB_TEST_DSN"


@pytest.fixture(scope="session")
def dsn() -> str:
    value = os.environ.get(DSN_ENV)
    if not value:
        pytest.skip(f"{DSN_ENV} が未設定のため統合テストを skip します")
    return value


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """スキーマを一度だけ用意する。

    **これが無いと、まっさらな DB でスイートが通らない。** API のテストは
    自分でスキーマを作らず、他のテストの `repo` フィクスチャが先に作って
    いることに依存していた。同じ DB を使い回している間は表が残るので露見
    しないが、新しい DB を指すと 28 件が落ちる。

    `dsn` フィクスチャに依存させると、環境変数が無いときに autouse の
    skip が全テストへ波及する。ここでは自分で読んで、無ければ何もしない。
    """
    value = os.environ.get(DSN_ENV)
    if not value:
        return
    psycopg = pytest.importorskip("psycopg")
    from kb.db import ChunkRepository

    with psycopg.connect(value, autocommit=True) as c:
        ChunkRepository(c).create_schema()


@pytest.fixture
def conn(dsn):
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def repo(conn):
    from kb.db import ChunkRepository

    r = ChunkRepository(conn)
    r.create_schema()
    return r


#: テナント単位で消すべき表。**分割していない表は DROP では消えない**ので、
#: パーティションを落とすだけでは knowledge_index などに行が残り続ける
#: （開発 DB に 1,600 行以上溜まっていた）。
_TENANT_SCOPED_TABLES = (
    "knowledge_index",
    "knowledge_label_state",
    "app_notifications",
    "notification_rules",
    "sync_state",
    "conversations",
)


@pytest.fixture
def tenant_id(repo):
    """テスト毎に独立したテナント（＝独立したパーティション）を使う。"""
    tid = uuid.uuid4()
    repo.ensure_tenant(tid)
    yield tid
    # パーティションは表ごと落とす
    repo._conn.execute(f'DROP TABLE IF EXISTS "kc_{tid.hex}"')
    repo._conn.execute(f'DROP TABLE IF EXISTS "ke_{tid.hex}"')
    # 分割していない表は行を消す。残すと開発 DB が実態不明になっていく
    for table in _TENANT_SCOPED_TABLES:
        repo._conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tid,))
