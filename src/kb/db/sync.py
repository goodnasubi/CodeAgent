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


@dataclass(frozen=True)
class SyncFailure:
    """取り込みに失敗し続けている知識。"""

    kb_issue_id: str
    attempts: int
    last_error: str | None
    first_failed_at: datetime
    last_failed_at: datetime


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

    # ------------------------------------------------- 知識ごとの失敗回数

    def record_item_failure(
        self, *, tenant_id: UUID, kb_issue_id: str, error: str
    ) -> int:
        """1 件の失敗を数え、**通算の連続失敗回数**を返す。

        呼び出し側はこの回数を見て、まだ再試行するか隔離するかを決める。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_failures"
                " (tenant_id, kb_issue_id, attempts, last_error)"
                " VALUES (%s, %s, 1, %s)"
                " ON CONFLICT (tenant_id, kb_issue_id) DO UPDATE SET"
                "   attempts = sync_failures.attempts + 1,"
                "   last_error = EXCLUDED.last_error,"
                "   last_failed_at = now()"
                " RETURNING attempts",
                (tenant_id, kb_issue_id, error[:2000]),
            )
            return int(cur.fetchone()[0])

    def clear_item_failure(self, *, tenant_id: UUID, kb_issue_id: str) -> None:
        """取り込めたら記録を消す。**連続失敗回数なので途中で成功したら 0 に戻す。**

        隔離済みの知識が KB 側で直された場合、ここを通って自然に復帰する。
        """
        self._conn.execute(
            "DELETE FROM sync_failures WHERE tenant_id = %s AND kb_issue_id = %s",
            (tenant_id, kb_issue_id),
        )

    def failures(self, *, tenant_id: UUID, at_least: int = 1) -> list[SyncFailure]:
        """失敗中の知識を、失敗回数の多い順に返す。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT kb_issue_id, attempts, last_error, first_failed_at,"
                "       last_failed_at"
                " FROM sync_failures WHERE tenant_id = %s AND attempts >= %s"
                " ORDER BY attempts DESC, last_failed_at DESC",
                (tenant_id, at_least),
            )
            return [SyncFailure(*row) for row in cur.fetchall()]
