"""HTTP API の統合テスト。"""

import os
import uuid

import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.integration


@pytest.fixture
def client(dsn, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("KB_DSN", dsn)
    monkeypatch.setenv("KB_SECRET_KEY", Fernet.generate_key().decode())

    from kb.api import create_app

    with fastapi_testclient.TestClient(create_app()) as c:
        yield c


@pytest.fixture
def tenant(client, conn):
    response = client.post("/api/admin/tenants", json={"name": "検証用テナント"})
    assert response.status_code == 200
    tid = response.json()["id"]
    yield tid
    for table in (
        "conversations",
        "app_notifications",
        "notification_rules",
        "knowledge_label_state",
        "knowledge_index",
        "sync_state",
        "tenant_kb_connections",
        "tenant_model_settings",
        "accounts",
    ):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tid,))
    conn.execute(f'DROP TABLE IF EXISTS "kc_{uuid.UUID(tid).hex}"')
    conn.execute(f'DROP TABLE IF EXISTS "ke_{uuid.UUID(tid).hex}"')
    conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


# ------------------------------------------------------------- テナント


def test_tenant_can_be_provisioned_and_listed(client, tenant):
    names = [t["name"] for t in client.get("/api/admin/tenants").json()]
    assert "検証用テナント" in names


def test_account_creation_requires_an_existing_tenant(client):
    missing = uuid.uuid4()
    response = client.post(
        f"/api/admin/tenants/{missing}/accounts", json={"display_name": "誰か"}
    )
    assert response.status_code == 404


def test_accounts_are_listed_per_tenant(client, tenant):
    client.post(f"/api/admin/tenants/{tenant}/accounts", json={"display_name": "山田"})
    accounts = client.get(f"/api/tenants/{tenant}/accounts").json()
    assert [a["display_name"] for a in accounts] == ["山田"]


# --------------------------------------------------------------- 設定


def test_kb_token_is_never_returned(client, tenant):
    """トークンをブラウザに渡さないこと。"""
    client.put(
        f"/api/tenants/{tenant}/kb",
        json={
            "kb_type": "github",
            "project": "acme/kb",
            "token": "とても秘密のトークン",
        },
    )
    body = client.get(f"/api/tenants/{tenant}/kb").json()

    assert body["kb_type"] == "github"
    assert body["project"] == "acme/kb"
    assert "token" not in body
    assert "とても秘密" not in str(body)


def test_kb_capabilities_are_exposed_for_the_ui(client, tenant):
    """画面が「この KB では使えない検索」を灰色にできるようにする。"""
    client.put(
        f"/api/tenants/{tenant}/kb",
        json={
            "kb_type": "relation",
            "project": "acme",
            "token": "t",
            "extra": {"message_box_id": 3},
        },
    )
    body = client.get(f"/api/tenants/{tenant}/kb").json()
    assert body["supports_keyword_search"] is False
    assert body["supports_relations"] is False


def test_unknown_kb_type_is_rejected(client, tenant):
    response = client.put(
        f"/api/tenants/{tenant}/kb",
        json={"kb_type": "jira", "project": "x", "token": "t"},
    )
    assert response.status_code == 400


def test_kb_returns_null_before_configuration(client, tenant):
    assert client.get(f"/api/tenants/{tenant}/kb").json() is None


def test_model_settings_roundtrip(client, tenant):
    client.put(
        f"/api/tenants/{tenant}/models",
        json={
            "llm_provider": "gemini",
            "llm_model": "gemini-2.5-flash",
            "embedding_provider": "hashing",
            "embedding_model": "hashing-dev",
            "embedding_dim": 256,
        },
    )
    body = client.get(f"/api/tenants/{tenant}/models").json()
    assert body["llm_provider"] == "gemini"
    assert body["embedding_dim"] == 256


# --------------------------------------------------------------- 検索


def test_search_works_without_a_configured_kb(client, tenant):
    """KB 未設定でも類似度検索は動く（pgvector 側で完結するため）。"""
    response = client.post(
        f"/api/tenants/{tenant}/search", json={"query": "ORA-01555"}
    )
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_search_reports_which_signals_were_skipped(client, tenant):
    body = client.post(f"/api/tenants/{tenant}/search", json={"query": "何か"}).json()
    assert "keyword" in body["skipped"]


def test_registering_without_a_kb_is_rejected(client, tenant):
    """知識の正は KB 側。設定が無ければ登録は成立しない。"""
    response = client.post(
        f"/api/tenants/{tenant}/knowledge", json={"title": "題", "body": "本文"}
    )
    assert response.status_code == 400


# --------------------------------------------------------- 素材の取り込み


def test_uploaded_file_comes_back_as_text(client, tenant):
    response = client.post(
        f"/api/tenants/{tenant}/extract/file",
        files={"file": ("log.txt", "ORA-01555 が出ました".encode(), "text/plain")},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["source_name"] == "log.txt"
    assert "ORA-01555" in body["text"]
    assert body["truncated"] is False


def test_long_extraction_is_cut_and_says_so(client, tenant, monkeypatch):
    """KB 側の本文長を超える素材は、黙って弾かれるのでなく切って知らせる。"""
    import kb.api

    monkeypatch.setattr(kb.api, "MAX_EXTRACTED_CHARS", 50)
    response = client.post(
        f"/api/tenants/{tenant}/extract/file",
        files={"file": ("long.txt", b"a" * 500, "text/plain")},
    )
    body = response.json()

    assert len(body["text"]) == 50
    assert body["truncated"] is True


def test_extraction_does_not_touch_the_kb(client, tenant):
    """取り込みは素材を返すだけ。KB 未設定でも動かないと検索の前段にならない。"""
    assert client.get(f"/api/tenants/{tenant}/kb").json() is None
    response = client.post(
        f"/api/tenants/{tenant}/extract/file",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200


def test_non_http_url_is_refused(client, tenant):
    """file:// を通すとサーバー上のファイルが読めてしまう。"""
    response = client.post(
        f"/api/tenants/{tenant}/extract/url", json={"url": "file:///etc/hostname"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------- 通知


def test_notification_rules_crud(client, tenant):
    rule = {"label": "重大", "channel": "in_app", "destination": ""}
    client.post(f"/api/tenants/{tenant}/notification-rules", json=rule)
    assert client.get(f"/api/tenants/{tenant}/notification-rules").json() == [rule]

    client.request(
        "DELETE", f"/api/tenants/{tenant}/notification-rules", json=rule
    )
    assert client.get(f"/api/tenants/{tenant}/notification-rules").json() == []


def test_notifications_start_empty(client, tenant):
    assert client.get(f"/api/tenants/{tenant}/notifications").json() == []


def test_mark_read_with_no_ids(client, tenant):
    response = client.post(
        f"/api/tenants/{tenant}/notifications/read", json={"ids": []}
    )
    assert response.json() == {"updated": 0}


# ------------------------------------------------------------ 会話履歴


def test_conversation_is_stored_server_side(client, tenant):
    """会話履歴は DB が正。端末を跨いでも続けられるようにするため。"""
    account = client.post(
        f"/api/admin/tenants/{tenant}/accounts", json={"display_name": "山田"}
    ).json()

    created = client.post(
        f"/api/tenants/{tenant}/conversations", params={"account_id": account["id"]}
    ).json()
    cid = created["id"]

    client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "プリンターが動きません"},
    )
    client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": "似た記録を3件見つけました"},
    )

    messages = client.get(f"/api/conversations/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "プリンターが動きません"


def test_first_user_message_becomes_the_title(client, tenant):
    """一覧で会話を見分けられるようにする。"""
    account = client.post(
        f"/api/admin/tenants/{tenant}/accounts", json={"display_name": "山田"}
    ).json()
    cid = client.post(
        f"/api/tenants/{tenant}/conversations", params={"account_id": account["id"]}
    ).json()["id"]

    client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "ORA-01555 の対処法を教えて"},
    )
    client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "二通目"},
    )

    conversations = client.get(
        f"/api/tenants/{tenant}/conversations", params={"account_id": account["id"]}
    ).json()
    assert conversations[0]["title"] == "ORA-01555 の対処法を教えて"


def test_unknown_role_is_rejected(client, tenant):
    account = client.post(
        f"/api/admin/tenants/{tenant}/accounts", json={"display_name": "山田"}
    ).json()
    cid = client.post(
        f"/api/tenants/{tenant}/conversations", params={"account_id": account["id"]}
    ).json()["id"]

    response = client.post(
        f"/api/conversations/{cid}/messages", json={"role": "system", "content": "x"}
    )
    assert response.status_code == 400


def test_conversations_are_scoped_to_the_account(client, tenant):
    a = client.post(
        f"/api/admin/tenants/{tenant}/accounts", json={"display_name": "山田"}
    ).json()
    b = client.post(
        f"/api/admin/tenants/{tenant}/accounts", json={"display_name": "佐藤"}
    ).json()
    client.post(f"/api/tenants/{tenant}/conversations", params={"account_id": a["id"]})

    assert (
        client.get(
            f"/api/tenants/{tenant}/conversations", params={"account_id": b["id"]}
        ).json()
        == []
    )


# --------------------------------------------------------- 開発者向け


def test_sync_status_before_any_run(client, tenant):
    body = client.get(f"/api/admin/tenants/{tenant}/sync").json()
    assert body["last_synced_at"] is None


def test_sync_status_reports_the_polling_interval(client, tenant):
    """常駐スケジューラが何分おきに動くはずかを画面に出せること。"""
    body = client.get(f"/api/admin/tenants/{tenant}/sync").json()
    assert body["interval_seconds"] == 600


def test_sync_without_a_kb_is_rejected(client, tenant):
    response = client.post(f"/api/admin/tenants/{tenant}/sync")
    assert response.status_code == 400


def test_manual_sync_refuses_to_run_alongside_the_scheduler(client, tenant, dsn):
    """常駐スケジューラが取り込み中のテナントは、手動実行を受け付けない。

    同時に走るとラベルの差分を両方が「新しく付いた」と判定し、通知が二度飛ぶ。
    """
    import psycopg

    from kb.db.sync import tenant_sync_lock

    with psycopg.connect(dsn, autocommit=True) as other:
        with tenant_sync_lock(other, tenant_id=uuid.UUID(tenant)):
            response = client.post(f"/api/admin/tenants/{tenant}/sync")

    assert response.status_code == 409
