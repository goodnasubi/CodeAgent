"""会話履歴の読み書き。

**DB が正**で、ブラウザの localStorage はキャッシュ。端末を跨いでも同じ
会話を続けられるようにするため。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import psycopg

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    id: int
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class Conversation:
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def create(
        self, *, tenant_id: UUID, account_id: UUID, title: str = ""
    ) -> Conversation:
        cid = uuid4()
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, tenant_id, account_id, title)"
                " VALUES (%s, %s, %s, %s) RETURNING created_at, updated_at",
                (cid, tenant_id, account_id, title),
            )
            created, updated = cur.fetchone()
        return Conversation(id=cid, title=title, created_at=created, updated_at=updated)

    def list_for_account(
        self, *, tenant_id: UUID, account_id: UUID, limit: int = 50
    ) -> list[Conversation]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, created_at, updated_at FROM conversations"
                " WHERE tenant_id = %s AND account_id = %s"
                " ORDER BY updated_at DESC LIMIT %s",
                (tenant_id, account_id, limit),
            )
            return [Conversation(*row) for row in cur.fetchall()]

    def messages(self, *, conversation_id: UUID) -> list[Message]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, role, content, created_at FROM conversation_messages"
                " WHERE conversation_id = %s ORDER BY created_at, id",
                (conversation_id,),
            )
            return [Message(*row) for row in cur.fetchall()]

    def add_message(
        self, *, conversation_id: UUID, role: str, content: str
    ) -> Message:
        if role not in (ROLE_USER, ROLE_ASSISTANT):
            raise ValueError(f"未知の role です: {role}")
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content)"
                " VALUES (%s, %s, %s) RETURNING id, created_at",
                (conversation_id, role, content),
            )
            mid, created = cur.fetchone()
            cur.execute(
                "UPDATE conversations SET updated_at = now() WHERE id = %s",
                (conversation_id,),
            )
        return Message(id=mid, role=role, content=content, created_at=created)

    def set_title(self, *, conversation_id: UUID, title: str) -> None:
        self._conn.execute(
            "UPDATE conversations SET title = %s WHERE id = %s",
            (title, conversation_id),
        )

    def delete(self, *, conversation_id: UUID) -> None:
        self._conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
