"""ポーリングの進捗の読み書き。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg


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
