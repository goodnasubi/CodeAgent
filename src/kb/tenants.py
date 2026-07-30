"""テナントの設定（KB 接続・モデル選択・アカウント）。

KB のトークンはアプリ側で暗号化して保存する。ブラウザには決して渡さない。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "KB_SECRET_KEY"

KIND_LLM = "llm"
KIND_EMBEDDING = "embedding"

#: API キーを入れる列。**呼び出し側の文字列をそのまま SQL に混ぜない**ための対応表。
_API_KEY_COLUMNS = {
    KIND_LLM: "encrypted_llm_api_key",
    KIND_EMBEDDING: "encrypted_embedding_api_key",
}


class SecretKeyMissing(RuntimeError):
    """暗号鍵が設定されていない。"""


class TokenUnreadable(RuntimeError):
    """保存済みトークンを復号できない。鍵が変わった可能性がある。"""


def load_cipher(key: str | None = None) -> Fernet:
    """トークン暗号化用の鍵を読む。

    鍵が無いまま起動して、あとからトークン保存で初めて落ちるより、
    起動時に気づける方がよい。
    """
    raw = key or os.environ.get(ENV_KEY)
    if not raw:
        raise SecretKeyMissing(
            f"{ENV_KEY} が未設定です。`python -c \"from cryptography.fernet import"
            ' Fernet; print(Fernet.generate_key().decode())"` で生成してください'
        )
    return Fernet(raw.encode() if isinstance(raw, str) else raw)


@dataclass(frozen=True)
class Tenant:
    id: UUID
    name: str


@dataclass(frozen=True)
class KbConnection:
    kb_type: str
    project: str
    base_url: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelSettings:
    llm_provider: str = "claude"
    llm_model: str = ""
    embedding_provider: str = "hashing"
    embedding_model: str = "hashing-dev"
    embedding_dim: int = 768


@dataclass(frozen=True)
class Account:
    id: UUID
    tenant_id: UUID
    display_name: str


class TenantRepository:
    def __init__(self, conn: psycopg.Connection, *, cipher: Fernet) -> None:
        self._conn = conn
        self._cipher = cipher

    # ---------------------------------------------------------- tenants

    def create_tenant(self, *, name: str, tenant_id: UUID | None = None) -> Tenant:
        tid = tenant_id or uuid4()
        self._conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s)", (tid, name)
        )
        return Tenant(id=tid, name=name)

    def list_tenants(self) -> list[Tenant]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name FROM tenants ORDER BY created_at")
            return [Tenant(id=r[0], name=r[1]) for r in cur.fetchall()]

    def get_tenant(self, tenant_id: UUID) -> Tenant | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name FROM tenants WHERE id = %s", (tenant_id,))
            row = cur.fetchone()
            return Tenant(id=row[0], name=row[1]) if row else None

    # --------------------------------------------------- KB connection

    def set_kb_connection(
        self, *, tenant_id: UUID, connection: KbConnection, token: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO tenant_kb_connections"
            " (tenant_id, kb_type, base_url, project, encrypted_token, extra)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id) DO UPDATE SET"
            "   kb_type = EXCLUDED.kb_type, base_url = EXCLUDED.base_url,"
            "   project = EXCLUDED.project, encrypted_token = EXCLUDED.encrypted_token,"
            "   extra = EXCLUDED.extra, updated_at = now()",
            (
                tenant_id,
                connection.kb_type,
                connection.base_url,
                connection.project,
                self._cipher.encrypt(token.encode()),
                json.dumps(connection.extra or {}),
            ),
        )

    def get_kb_connection(self, tenant_id: UUID) -> KbConnection | None:
        """接続設定を返す。**トークンは含まない。**

        画面表示や一覧のためにここを呼ぶ場面が多いので、既定では復号しない。
        トークンが要るときだけ get_kb_token を呼ぶ。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT kb_type, project, base_url, extra FROM tenant_kb_connections"
                " WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return KbConnection(
                kb_type=row[0], project=row[1], base_url=row[2], extra=row[3] or {}
            )

    def get_kb_token(self, tenant_id: UUID) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT encrypted_token FROM tenant_kb_connections WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            try:
                return self._cipher.decrypt(bytes(row[0])).decode()
            except InvalidToken as exc:
                raise TokenUnreadable(
                    "保存済みトークンを復号できません。"
                    f"{ENV_KEY} が変わった可能性があります"
                ) from exc

    # ------------------------------------------------- model settings

    def set_model_settings(self, *, tenant_id: UUID, settings: ModelSettings) -> None:
        self._conn.execute(
            "INSERT INTO tenant_model_settings"
            " (tenant_id, llm_provider, llm_model, embedding_provider,"
            "  embedding_model, embedding_dim)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id) DO UPDATE SET"
            "   llm_provider = EXCLUDED.llm_provider, llm_model = EXCLUDED.llm_model,"
            "   embedding_provider = EXCLUDED.embedding_provider,"
            "   embedding_model = EXCLUDED.embedding_model,"
            "   embedding_dim = EXCLUDED.embedding_dim, updated_at = now()",
            (
                tenant_id,
                settings.llm_provider,
                settings.llm_model,
                settings.embedding_provider,
                settings.embedding_model,
                settings.embedding_dim,
            ),
        )

    def set_model_api_key(self, *, tenant_id: UUID, kind: str, api_key: str) -> None:
        """LLM / embedding プロバイダの API キーを保存する。

        モデル選択（set_model_settings）とは別の操作にしてある。設定画面で
        プロバイダ名だけ直したいときに、キーを空で上書きして消してしまう
        事故を防ぐため。
        """
        column = _API_KEY_COLUMNS[kind]
        self._conn.execute(
            f"INSERT INTO tenant_model_settings (tenant_id, {column})"
            " VALUES (%s, %s)"
            " ON CONFLICT (tenant_id) DO UPDATE SET"
            f"   {column} = EXCLUDED.{column}, updated_at = now()",
            (tenant_id, self._cipher.encrypt(api_key.encode())),
        )

    def get_model_api_key(self, tenant_id: UUID, kind: str) -> str | None:
        column = _API_KEY_COLUMNS[kind]
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {column} FROM tenant_model_settings WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            try:
                return self._cipher.decrypt(bytes(row[0])).decode()
            except InvalidToken as exc:
                raise TokenUnreadable(
                    "保存済みの API キーを復号できません。"
                    f"{ENV_KEY} が変わった可能性があります"
                ) from exc

    def get_model_settings(self, tenant_id: UUID) -> ModelSettings:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT llm_provider, llm_model, embedding_provider, embedding_model,"
                " embedding_dim FROM tenant_model_settings WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            return ModelSettings(*row) if row else ModelSettings()

    # ---------------------------------------------------------- accounts

    def create_account(
        self, *, tenant_id: UUID, display_name: str, account_id: UUID | None = None
    ) -> Account:
        aid = account_id or uuid4()
        self._conn.execute(
            "INSERT INTO accounts (id, tenant_id, display_name) VALUES (%s, %s, %s)",
            (aid, tenant_id, display_name),
        )
        return Account(id=aid, tenant_id=tenant_id, display_name=display_name)

    def get_account(self, account_id: UUID) -> Account | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, tenant_id, display_name FROM accounts WHERE id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            return Account(id=row[0], tenant_id=row[1], display_name=row[2]) if row else None

    def list_accounts(self, tenant_id: UUID) -> list[Account]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, tenant_id, display_name FROM accounts"
                " WHERE tenant_id = %s ORDER BY created_at",
                (tenant_id,),
            )
            return [
                Account(id=r[0], tenant_id=r[1], display_name=r[2])
                for r in cur.fetchall()
            ]
