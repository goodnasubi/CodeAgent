"""Re:lation アダプタの単体テスト。HTTP はモックする。

**注意**: Re:lation は SaaS で手元に立てられないため、実機での疎通確認は
まだ行っていない。他の 3 バックエンドでは、モックでは見つからない齟齬を
実機テストが検出している（GitHub の PR 混入、GitLab の work_items URL、
Redmine の status_id など）。契約後に tests/test_backends_relation_live.py
を追加して確認すること。
"""

from datetime import datetime, timezone

import httpx
import pytest

from kb.backends import KnowledgeBase, KnowledgeBaseError, KnowledgeNotFound
from kb.backends.base import UnsupportedOperation
from kb.backends.relation import RelationKnowledgeBase

SUB = "acme"
BOX = 3
BASE = f"https://{SUB}.relationapp.jp/api/v2/{BOX}"


def build(handler) -> RelationKnowledgeBase:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RelationKnowledgeBase(
        access_token="t", subdomain=SUB, message_box_id=BOX, client=client
    )


def ticket(tid=1, *, title="題名", messages=None, comments=None, labels=None):
    return {
        "ticket_id": tid,
        "title": title,
        "status_cd": "closed",
        "last_updated_at": "2026-07-29T01:02:03Z",
        "messages": messages if messages is not None else [{"body": "本文"}],
        "comments": comments or [],
        "labels": labels or [],
    }


def test_satisfies_protocol():
    assert isinstance(build(lambda r: httpx.Response(200, json={})), KnowledgeBase)


def test_capabilities_are_declared_false():
    """出来ないことを型で表す。呼び出し側はこれを見て検索を飛ばす。"""
    assert RelationKnowledgeBase.supports_keyword_search is False
    assert RelationKnowledgeBase.supports_relations is False


def test_base_url_includes_subdomain_and_message_box():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=ticket(1))

    build(handler).get("1")
    assert seen["url"] == f"{BASE}/tickets/1"


@pytest.mark.parametrize("kwargs", [{"subdomain": ""}, {"message_box_id": ""}])
def test_required_settings_are_validated(kwargs):
    base = {"access_token": "t", "subdomain": SUB, "message_box_id": BOX}
    with pytest.raises(ValueError):
        RelationKnowledgeBase(**{**base, **kwargs})


def test_first_message_is_body_and_the_rest_become_comments():
    """チケットはやり取りの束。全体が combined_text に入るようにする。"""
    payload = ticket(
        1,
        messages=[{"body": "最初の問い合わせ"}, {"body": "二通目の返信"}],
        comments=[{"comment": "社内メモ"}],
    )
    k = build(lambda r: httpx.Response(200, json=payload)).get("1")

    assert k.body == "最初の問い合わせ"
    assert k.comments == ("二通目の返信", "社内メモ")
    combined = k.combined_text()
    assert all(t in combined for t in ("最初の問い合わせ", "二通目の返信", "社内メモ"))


def test_empty_messages_are_skipped():
    payload = ticket(1, messages=[{"body": "  "}, {"body": "中身"}], comments=[{"comment": ""}])
    k = build(lambda r: httpx.Response(200, json=payload)).get("1")
    assert k.body == "中身"
    assert k.comments == ()


def test_create_posts_a_record_without_ticket_id():
    """チケットを直接作る API が無いので、ticket_id を省いた record で作る。"""
    seen = {}

    def handler(request):
        import json

        if request.url.path.endswith("/records"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"message_id": 9, "ticket_id": 16})
        return httpx.Response(200, json=ticket(16))

    result = build(handler).create(title="新しい知識", body="内容")

    assert "ticket_id" not in seen["body"], "既存チケットに追記してしまっている"
    assert seen["body"]["subject"] == "新しい知識"
    assert seen["body"]["body"] == "内容"
    assert seen["body"]["duration"] == 0
    assert result.id == "16"


def test_create_sends_a_non_future_timestamp():
    """operated_at は過去（現在含む）でなければ受け付けられない。"""
    seen = {}

    def handler(request):
        import json

        if request.url.path.endswith("/records"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ticket_id": 1})
        return httpx.Response(200, json=ticket(1))

    build(handler).create(title="題", body="本文")
    sent = datetime.strptime(seen["body"]["operated_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert sent.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)


def test_create_requires_title():
    with pytest.raises(ValueError):
        build(lambda r: httpx.Response(200, json={})).create(title="  ", body="x")


def test_create_fails_loudly_without_ticket_id():
    def handler(request):
        return httpx.Response(200, json={"message_id": 9})

    with pytest.raises(KnowledgeBaseError, match="ticket_id"):
        build(handler).create(title="題", body="本文")


def test_append_targets_the_existing_ticket():
    seen = {}

    def handler(request):
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ticket_id": 5})

    build(handler).append("5", "あとから分かったこと")
    assert seen["body"]["ticket_id"] == 5
    assert seen["body"]["body"] == "あとから分かったこと"


def test_append_rejects_empty():
    with pytest.raises(ValueError):
        build(lambda r: httpx.Response(200, json={})).append("5", " ")


def test_add_labels_resolves_names_to_ids_and_keeps_existing():
    """Re:lation はラベルを ID で扱い、指定は置き換えになる。"""
    sent = {}

    def handler(request):
        import json

        path = request.url.path
        if path.endswith("/labels"):
            return httpx.Response(
                200,
                json=[
                    {"label_id": 10, "name": "bug"},
                    {"label_id": 20, "name": "db"},
                ],
            )
        if request.method == "PUT":
            sent["body"] = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(200, json=ticket(1, labels=[{"name": "bug"}]))

    build(handler).add_labels("1", ["db"])
    assert sent["body"] == {"label_ids": [10, 20]}, "既存ラベルが消えている"


def test_add_labels_reports_unknown_label():
    def handler(request):
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json=[{"label_id": 10, "name": "bug"}])
        return httpx.Response(200, json=ticket(1))

    with pytest.raises(KnowledgeBaseError, match="存在しないラベル"):
        build(handler).add_labels("1", ["未登録"])


def test_add_labels_skips_request_when_empty():
    calls = []
    kb = build(lambda r: (calls.append(1), httpx.Response(200, json=ticket(1)))[1])
    kb.add_labels("1", [])
    assert calls == []


def test_updated_since_asks_for_all_relevant_statuses():
    """既定が不明なため明示する。対応完了こそ知識として欲しい。"""
    seen = {}

    def handler(request):
        import json

        if request.url.path.endswith("/search"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=ticket(1))

    kb = build(handler)
    list(kb.updated_since(datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)))

    assert seen["body"]["last_updated_since"] == "2026-07-29T12:00:00Z"
    assert "closed" in seen["body"]["status_cds"], "対応完了を取りこぼす"
    assert "trash" not in seen["body"]["status_cds"], "ゴミ箱は知識ではない"


def test_updated_since_paginates():
    pages = {1: [{"ticket_id": i} for i in range(50)], 2: [{"ticket_id": 99}]}

    def handler(request):
        import json

        if request.url.path.endswith("/search"):
            page = json.loads(request.content)["page"]
            return httpx.Response(200, json=pages.get(page, []))
        tid = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json=ticket(tid))

    kb = build(handler)
    ids = [k.id for k in kb.updated_since(datetime(2026, 7, 1, tzinfo=timezone.utc))]
    assert len(ids) == 51 and ids[-1] == "99"


def test_search_is_reported_as_unsupported():
    """黙って空を返すと「ヒット0件」と区別できない。"""
    with pytest.raises(UnsupportedOperation, match="キーワード検索"):
        build(lambda r: httpx.Response(200, json=[])).search("ORA-01555")


def test_relations_returns_empty_rather_than_raising():
    """つながりが無いのは正しい状態。検索自体は成立する。"""
    assert build(lambda r: httpx.Response(200, json={})).relations("1") == []


def test_rate_limit_reports_reset_time():
    kb = build(
        lambda r: httpx.Response(429, headers={"X-RateLimit-Reset": "1800000000"}, json={})
    )
    with pytest.raises(KnowledgeBaseError, match="1800000000"):
        kb.get("1")


def test_missing_ticket_raises_not_found():
    with pytest.raises(KnowledgeNotFound):
        build(lambda r: httpx.Response(404, json={})).get("999")


def test_bad_token_is_reported():
    with pytest.raises(KnowledgeBaseError, match="アクセストークン"):
        build(lambda r: httpx.Response(401, json={})).get("1")


def test_connection_error_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(KnowledgeBaseError, match="接続に失敗"):
        build(handler).get("1")
