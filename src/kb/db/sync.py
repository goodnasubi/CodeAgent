"""ポーリングの進捗の読み書き。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator
from uuid import UUID

import psycopg

#: advisory lock の名前空間。他の用途と鍵がぶつからないように前半を固定する
LOCK_NAMESPACE = 0x4B42  # 'KB'


class SyncInProgress(RuntimeError):
    """同じテナントの取り込みが既に走っている。"""


def _lock_key(tenant_id: UUID) -> int:
    """テナント ID を advisory lock の int4 鍵に落とす。

    衝突しても起きるのは「別テナントの取り込みが 1 周ぶん見送られる」だけで、
    次の周回で取り込まれる。int4 空間での偶発的な衝突を厳密に避ける価値はない。
    """
    return int.from_bytes(tenant_id.bytes[:4], "big", signed=True)


@contextmanager
def tenant_sync_lock(conn: psycopg.Connection, *, tenant_id: UUID) -> Iterator[None]:
    """テナント単位で取り込みの同時実行を防ぐ。

    常駐スケジューラと開発者画面の手動実行、あるいはスケジューラが二重に
    起動している場合に、同じテナントを同時に取り込むと**通知が二重に飛ぶ**。
    ラベルの差分検知は「前回見たラベル」を読んで比べて書き戻すため、
    2 つの取り込みが同時に読むと両方が「新しく付いた」と判定するため。

    取り込み自体（chunk の入れ替え）は同じ内容で上書きするので二重でも
    壊れないが、通知は取り消せない。

    セッション単位のロックなので、必ず解放する。取れなければ
    `SyncInProgress` を送出し、待たない（次の周回で拾えばよい）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, _lock_key(tenant_id))
        )
        acquired = cur.fetchone()[0]
    if not acquired:
        raise SyncInProgress("このテナントの取り込みは既に実行中です")
    try:
        yield
    finally:
        conn.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (LOCK_NAMESPACE, _lock_key(tenant_id)),
        )


@dataclass(frozen=True)
class SyncState:
    last_synced_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None


class SyncStateRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get(self, *, tenant_id: UUID) -> SyncState | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT last_synced_at, last_run_at, last_error FROM sync_state"
                " WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            return SyncState(*row) if row else None

    def record_success(self, *, tenant_id: UUID, synced_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO sync_state (tenant_id, last_synced_at, last_run_at, last_error)"
            " VALUES (%s, %s, now(), NULL)"
            " ON CONFLICT (tenant_id) DO UPDATE SET"
            "   last_synced_at = EXCLUDED.last_synced_at,"
            "   last_run_at = now(), last_error = NULL",
            (tenant_id, synced_at),
        )

    def record_failure(self, *, tenant_id: UUID, error: str) -> None:
        """失敗を記録する。**`last_synced_at` は進めない。**

        取り込めなかった分は次回もう一度取りに行く必要があるため。
        """
        self._conn.execute(
            "INSERT INTO sync_state (tenant_id, last_run_at, last_error)"
            " VALUES (%s, now(), %s)"
            " ON CONFLICT (tenant_id) DO UPDATE SET"
            "   last_run_at = now(), last_error = EXCLUDED.last_error",
            (tenant_id, error[:2000]),
        )
