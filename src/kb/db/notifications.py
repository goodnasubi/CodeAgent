"""通知まわりの読み書き。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from uuid import UUID

import psycopg


@dataclass(frozen=True)
class NotificationRule:
    label: str
    channel: str
    destination: str


@dataclass(frozen=True)
class AppNotification:
    id: int
    kb_issue_id: str
    label: str
    title: str
    url: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------- rules

    def add_rule(
        self, *, tenant_id: UUID, label: str, channel: str, destination: str
    ) -> None:
        """ラベル → 通知先の対応を登録する。同じ組み合わせの重複は無視する。"""
        self._conn.execute(
            "INSERT INTO notification_rules (tenant_id, label, channel, destination)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, label, channel, destination) DO NOTHING",
            (tenant_id, label, channel, destination),
        )

    def remove_rule(
        self, *, tenant_id: UUID, label: str, channel: str, destination: str
    ) -> None:
        self._conn.execute(
            "DELETE FROM notification_rules WHERE tenant_id = %s AND label = %s"
            " AND channel = %s AND destination = %s",
            (tenant_id, label, channel, destination),
        )

    def rules_for(self, *, tenant_id: UUID, label: str) -> list[NotificationRule]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT label, channel, destination FROM notification_rules"
                " WHERE tenant_id = %s AND label = %s ORDER BY channel, destination",
                (tenant_id, label),
            )
            return [NotificationRule(*row) for row in cur.fetchall()]

    def all_rules(self, *, tenant_id: UUID) -> list[NotificationRule]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT label, channel, destination FROM notification_rules"
                " WHERE tenant_id = %s ORDER BY label, channel, destination",
                (tenant_id,),
            )
            return [NotificationRule(*row) for row in cur.fetchall()]

    # ------------------------------------------------------- label state

    def known_labels(self, *, tenant_id: UUID, kb_issue_id: str) -> tuple[str, ...] | None:
        """前回の同期時点で付いていたラベル。未取り込みなら None。

        None（未取り込み）と空タプル（ラベルが無い状態を取り込み済み）は
        区別する。初回取り込みで既存の全ラベルを「新規付与」として通知して
        しまうと、運用開始時に大量の通知が飛ぶため。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT labels FROM knowledge_label_state"
                " WHERE tenant_id = %s AND kb_issue_id = %s",
                (tenant_id, kb_issue_id),
            )
            row = cur.fetchone()
            return tuple(row[0]) if row else None

    def remember_labels(
        self, *, tenant_id: UUID, kb_issue_id: str, labels: Sequence[str]
    ) -> None:
        self._conn.execute(
            "INSERT INTO knowledge_label_state (tenant_id, kb_issue_id, labels)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (tenant_id, kb_issue_id)"
            " DO UPDATE SET labels = EXCLUDED.labels, updated_at = now()",
            (tenant_id, kb_issue_id, list(labels)),
        )

    # --------------------------------------------------- in-app inbox

    def record_app_notification(
        self,
        *,
        tenant_id: UUID,
        kb_issue_id: str,
        label: str,
        title: str,
        url: str | None,
    ) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_notifications"
                " (tenant_id, kb_issue_id, label, title, url)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (tenant_id, kb_issue_id, label, title, url),
            )
            return int(cur.fetchone()[0])

    def unread(self, *, tenant_id: UUID, limit: int = 50) -> list[AppNotification]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, kb_issue_id, label, title, url, created_at, read_at"
                " FROM app_notifications WHERE tenant_id = %s AND read_at IS NULL"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (tenant_id, limit),
            )
            return [AppNotification(*row) for row in cur.fetchall()]

    def mark_read(self, *, tenant_id: UUID, notification_ids: Sequence[int]) -> int:
        if not notification_ids:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE app_notifications SET read_at = now()"
                " WHERE tenant_id = %s AND id = ANY(%s) AND read_at IS NULL",
                (tenant_id, list(notification_ids)),
            )
            return cur.rowcount
