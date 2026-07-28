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


@pytest.fixture
def tenant_id(repo):
    """テスト毎に独立したテナント（＝独立したパーティション）を使う。"""
    tid = uuid.uuid4()
    repo.ensure_tenant(tid)
    yield tid
    repo._conn.execute(f'DROP TABLE IF EXISTS "kc_{tid.hex}"')
