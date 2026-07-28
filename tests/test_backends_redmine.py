"""Redmine アダプタの単体テスト。HTTP はモックする。"""

from datetime import datetime, timezone

import httpx
import pytest

from kb.backends import KnowledgeBase, KnowledgeBaseError, KnowledgeNotFound
from kb.backends.redmine import LabelFieldMissing, RedmineKnowledgeBase

BASE = "https://redmine.example.co.jp"
PROJECT = "kb"


def build(handler, **kwargs) -> RedmineKnowledgeBase:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RedmineKnowledgeBase(
        api_key="k", base_url=BASE, project=PROJECT, client=client, **kwargs
    )


def issue(iid=1, *, subject="題名", description="本文", journals=None, labels=None):
    payload = {
        "id": iid,
        "subject": subject,
        "description": description,
        "updated_on": "2026-07-29T01:02:03Z",
    }
    if journals is not None:
        payload["journals"] = journals
    if labels is not None:
        payload["custom_fields"] = [{"id": 1, "name": "Labels", "value": labels}]
    return payload


def test_satisfies_protocol():
    assert isinstance(build(lambda r: httpx.Response(200, json={})), KnowledgeBase)


def test_base_url_is_required():
    with pytest.raises(ValueError):
        RedmineKnowledgeBase(api_key="k", base_url="", project=PROJECT)


def test_project_is_required():
    with pytest.raises(ValueError):
        RedmineKnowledgeBase(api_key="k", base_url=BASE, project="")


def test_url_is_built_from_base():
    kb = build(lambda r: httpx.Response(200, json={"issue": issue(5)}))
    assert kb.get("5").url == f"{BASE}/issues/5"


def test_property_only_journals_are_excluded():
    """優先度変更など、注記の無い journal を知識に混ぜない。"""
    journals = [
        {"notes": "人が書いた追記", "details": []},
        {"notes": "", "details": [{"name": "priority_id"}]},
        {"notes": "   ", "details": []},
    ]
    kb = build(lambda r: httpx.Response(200, json={"issue": issue(1, journals=journals)}))
    assert kb.get("1").comments == ("人が書いた追記",)


def test_get_requests_journals():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"issue": issue(1)})

    build(handler).get("1")
    assert seen["params"].get("include") == "journals"


def test_append_posts_notes_without_touching_description():
    seen = {}

    def handler(request):
        import json

        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    build(handler).append("5", "追記の内容")
    assert seen["method"] == "PUT"
    assert seen["body"] == {"issue": {"notes": "追記の内容"}}
    assert "description" not in seen["body"]["issue"]


def test_append_rejects_empty():
    with pytest.raises(ValueError):
        build(lambda r: httpx.Response(200, json={})).append("5", " ")


def test_labels_read_from_custom_field():
    kb = build(
        lambda r: httpx.Response(200, json={"issue": issue(1, labels=["bug", "db"])})
    )
    assert kb.get("1").labels == ("bug", "db")


def test_missing_label_field_is_reported_clearly():
    """Redmine にラベル機能は無い。設定漏れを黙って無視しない。"""

    def handler(request):
        if "custom_fields" in str(request.url):
            return httpx.Response(200, json={"custom_fields": []})
        return httpx.Response(200, json={"issue": issue(1)})

    with pytest.raises(LabelFieldMissing, match="Labels"):
        build(handler).add_labels("1", ["bug"])


def test_add_labels_merges_with_existing():
    """カスタムフィールドは置き換えなので、既存値を残す必要がある。"""
    sent = {}

    def handler(request):
        import json

        url = str(request.url)
        if "custom_fields" in url:
            return httpx.Response(
                200, json={"custom_fields": [{"id": 7, "name": "Labels"}]}
            )
        if request.method == "PUT":
            sent["body"] = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"issue": issue(1, labels=["bug"])})

    build(handler).add_labels("1", ["db"])
    assert sent["body"]["issue"]["custom_fields"] == [{"id": 7, "value": ["bug", "db"]}]


def test_add_labels_skips_request_when_empty():
    calls = []
    kb = build(lambda r: (calls.append(1), httpx.Response(200, json={}))[1])
    kb.add_labels("1", [])
    assert calls == []


def test_updated_since_includes_closed_issues():
    """status_id=* が無いと未完了しか返らず、解決済みの知識を取りこぼす。"""
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"issues": [], "total_count": 0})

    kb = build(handler, fetch_comments=False)
    list(kb.updated_since(datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)))
    assert seen["params"]["status_id"] == "*"
    assert seen["params"]["updated_on"] == ">=2026-07-29T12:00:00Z"


def test_paging_follows_total_count():
    pages = [
        {"issues": [issue(1), issue(2)], "total_count": 3},
        {"issues": [issue(3)], "total_count": 3},
    ]

    def handler(request):
        offset = int(dict(request.url.params).get("offset", 0))
        return httpx.Response(200, json=pages[0] if offset == 0 else pages[1])

    kb = build(handler, fetch_comments=False)
    ids = [k.id for k in kb.updated_since(datetime(2026, 7, 1, tzinfo=timezone.utc))]
    assert ids == ["1", "2", "3"]


def test_relations_resolve_the_other_side():
    """関連は 1 レコードで双方向を表す。自分がどちら側かを見て相手を決める。"""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "relations": [
                    {"issue_id": 1, "issue_to_id": 2, "relation_type": "relates"},
                    {"issue_id": 3, "issue_to_id": 1, "relation_type": "blocks"},
                ]
            },
        )

    rels = {r.to_id: r.kind for r in build(handler).relations("1")}
    assert rels == {"2": "relates", "3": "blocks"}


def test_relations_empty():
    kb = build(lambda r: httpx.Response(200, json={"relations": []}))
    assert kb.relations("1") == []


def test_missing_issue_raises_not_found():
    with pytest.raises(KnowledgeNotFound):
        build(lambda r: httpx.Response(404, json={})).get("999")


def test_bad_api_key_is_reported():
    with pytest.raises(KnowledgeBaseError, match="API キー"):
        build(lambda r: httpx.Response(401, json={})).get("1")


def test_rest_api_disabled_is_reported():
    with pytest.raises(KnowledgeBaseError, match="REST API"):
        build(lambda r: httpx.Response(403, json={})).get("1")


def test_connection_error_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(KnowledgeBaseError, match="接続に失敗"):
        build(handler).get("1")
