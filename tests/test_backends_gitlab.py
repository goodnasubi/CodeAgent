"""GitLab アダプタの単体テスト。HTTP はモックする。"""

from datetime import datetime, timezone

import httpx
import pytest

from kb.backends import KnowledgeBase, KnowledgeBaseError, KnowledgeNotFound
from kb.backends.gitlab import GitLabKnowledgeBase

PROJECT = "synapse-corporation-group/kb"
ENCODED = "synapse-corporation-group%2Fkb"


def build(handler) -> GitLabKnowledgeBase:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GitLabKnowledgeBase(token="t", project=PROJECT, client=client)


def issue(iid=1, *, title="題名", body="本文", labels=()):
    return {
        "id": 9000 + iid,  # インスタンス全体の ID。利用者には見えない
        "iid": iid,
        "title": title,
        "description": body,
        "web_url": f"https://gitlab.com/{PROJECT}/-/issues/{iid}",
        "labels": list(labels),
        "updated_at": "2026-07-29T01:02:03.000Z",
    }


def test_satisfies_protocol():
    assert isinstance(build(lambda r: httpx.Response(200, json=[])), KnowledgeBase)


def test_project_path_is_url_encoded():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(201, json=issue(1))

    build(handler).create(title="t", body="b")
    assert ENCODED in seen["url"], "プロジェクトパスがエンコードされていない"


def test_empty_project_rejected():
    with pytest.raises(ValueError):
        GitLabKnowledgeBase(token="t", project="")


def test_uses_iid_not_global_id():
    """利用者が目にする番号は iid。グローバル ID を返してはいけない。"""

    def handler(request):
        if "/notes" in str(request.url):
            return httpx.Response(200, json=[], headers={"X-Next-Page": ""})
        return httpx.Response(200, json=issue(7))

    assert build(handler).get("7").id == "7"


def test_create_sends_description_and_joined_labels():
    seen = {}

    def handler(request):
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=issue(3))

    build(handler).create(title="題", body="内容", labels=["bug", "db"])
    assert seen["body"]["description"] == "内容"
    assert seen["body"]["labels"] == "bug,db"


def test_get_excludes_system_notes():
    """「ラベルを追加した」等の操作履歴を知識に混ぜない。"""

    def handler(request):
        if "/notes" in str(request.url):
            return httpx.Response(
                200,
                json=[
                    {"body": "本物の追記", "system": False},
                    {"body": "added ~bug label", "system": True},
                ],
            )
        return httpx.Response(200, json=issue(3))

    k = build(handler).get("3")
    assert k.comments == ("本物の追記",), "システムノートが混入している"


def test_get_skips_blank_notes():
    def handler(request):
        if "/notes" in str(request.url):
            return httpx.Response(200, json=[{"body": "  ", "system": False}])
        return httpx.Response(200, json=issue(3))

    assert build(handler).get("3").comments == ()


def test_append_posts_note():
    seen = {}

    def handler(request):
        import json

        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={})

    build(handler).append("5", "あとから分かったこと")
    assert "/issues/5/notes" in seen["url"]
    assert seen["body"] == {"body": "あとから分かったこと"}


def test_append_rejects_empty():
    with pytest.raises(ValueError):
        build(lambda r: httpx.Response(201, json={})).append("5", " ")


def test_add_labels_uses_add_labels_to_avoid_replacing():
    """`labels` で送ると既存ラベルが消えるため使わない。"""
    seen = {}

    def handler(request):
        import json

        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=issue(5))

    build(handler).add_labels("5", ["urgent"])
    assert seen["method"] == "PUT"
    assert seen["body"] == {"add_labels": "urgent"}
    assert "labels" not in seen["body"], "既存ラベルを置き換えてしまう"


def test_add_labels_skips_request_when_empty():
    calls = []
    kb = build(lambda r: (calls.append(1), httpx.Response(200, json=issue()))[1])
    kb.add_labels("5", [])
    assert calls == []


def test_updated_since_sends_updated_after_in_utc():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    kb = build(handler)
    list(kb.updated_since(datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)))
    assert seen["params"]["updated_after"] == "2026-07-29T12:00:00Z"


def test_updated_since_follows_x_next_page():
    """GitLab は Link ヘッダに加えて X-Next-Page でも次ページを示す。"""
    pages = {1: ([issue(1)], "2"), 2: ([issue(2)], "")}

    def handler(request):
        page = int(dict(request.url.params).get("page", 1))
        if "/notes" in str(request.url):
            return httpx.Response(200, json=[], headers={"X-Next-Page": ""})
        body, nxt = pages[page]
        return httpx.Response(200, json=body, headers={"X-Next-Page": nxt})

    kb = build(handler)
    ids = [k.id for k in kb.updated_since(datetime(2026, 7, 1, tzinfo=timezone.utc))]
    assert ids == ["1", "2"]


def test_search_passes_query_and_limit():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[issue(1), issue(2), issue(3)])

    results = build(handler).search("ORA-01555", limit=2)
    assert seen["params"]["search"] == "ORA-01555"
    assert [k.id for k in results] == ["1", "2"]


def test_relations_reads_first_class_links():
    """GitLab は関連イシューを標準機能として持つ。"""

    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"iid": 2, "link_type": "relates_to"},
                {"iid": 3, "link_type": "blocks"},
            ],
            headers={"X-Next-Page": ""},
        )

    rels = build(handler).relations("1")
    assert {r.to_id for r in rels} == {"2", "3"}
    assert {r.kind for r in rels} == {"relates_to", "blocks"}


def test_relations_ignores_self_link():
    def handler(request):
        return httpx.Response(
            200, json=[{"iid": 1}, {"iid": 2}], headers={"X-Next-Page": ""}
        )

    assert {r.to_id for r in build(handler).relations("1")} == {"2"}


def test_relations_empty_when_none():
    kb = build(lambda r: httpx.Response(200, json=[], headers={"X-Next-Page": ""}))
    assert kb.relations("1") == []


def test_missing_issue_raises_not_found():
    with pytest.raises(KnowledgeNotFound):
        build(lambda r: httpx.Response(404, json={})).get("404")


def test_rate_limit_is_reported():
    with pytest.raises(KnowledgeBaseError, match="レート制限"):
        build(lambda r: httpx.Response(429, json={})).get("1")


def test_connection_error_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(KnowledgeBaseError, match="接続に失敗"):
        build(handler).get("1")


def test_self_hosted_base_url_is_honoured():
    seen = {}

    def handler(request):
        seen.setdefault("url", str(request.url))
        if "/notes" in str(request.url):
            return httpx.Response(200, json=[], headers={"X-Next-Page": ""})
        return httpx.Response(200, json=issue(1))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    kb = GitLabKnowledgeBase(
        token="t",
        project=PROJECT,
        api_base="https://gitlab.example.co.jp/api/v4",
        client=client,
    )
    kb.get("1")
    assert seen["url"].startswith("https://gitlab.example.co.jp/api/v4")


# ------------------------------------------ 古い/Free 版 GitLab への対応


def notes_response(*bodies_system):
    return httpx.Response(
        200,
        json=[{"body": b, "system": s} for b, s in bodies_system],
        headers={"X-Next-Page": ""},
    )


@pytest.mark.parametrize("status", [403, 404])
def test_relations_falls_back_when_links_api_unavailable(status):
    """関連イシュー API は Premium 機能。無い環境でも参照を拾えること。"""

    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(status, json={"message": "403 Forbidden"})
        if "/notes" in str(request.url):
            return notes_response(("mentioned in issue #42", True))
        return httpx.Response(200, json=issue(1))

    rels = build(handler).relations("1")
    assert {r.to_id for r in rels} == {"42"}
    assert rels[0].kind == "mentioned"


def test_relations_merges_links_api_and_system_notes():
    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(
                200, json=[{"iid": 2, "link_type": "blocks"}], headers={"X-Next-Page": ""}
            )
        if "/notes" in str(request.url):
            return notes_response(("mentioned in issue #3", True))
        return httpx.Response(200, json=issue(1))

    rels = {r.to_id: r.kind for r in build(handler).relations("1")}
    assert rels == {"2": "blocks", "3": "mentioned"}


def test_links_api_wins_over_system_note_for_the_same_issue():
    """両方から同じ相手が来たら、種類の分かる links 側を採る。"""

    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(
                200, json=[{"iid": 5, "link_type": "blocks"}], headers={"X-Next-Page": ""}
            )
        if "/notes" in str(request.url):
            return notes_response(("mentioned in issue #5", True))
        return httpx.Response(200, json=issue(1))

    rels = build(handler).relations("1")
    assert len(rels) == 1
    assert rels[0].kind == "blocks"


def test_relations_reads_marked_as_related_note():
    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(403, json={})
        if "/notes" in str(request.url):
            return notes_response(("marked this issue as related to #8", True))
        return httpx.Response(200, json=issue(1))

    assert {r.to_id for r in build(handler).relations("1")} == {"8"}


def test_relations_ignores_cross_project_mentions():
    """他プロジェクトの番号は拾わない（iid はプロジェクト内の連番のため）。"""

    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(403, json={})
        if "/notes" in str(request.url):
            return notes_response(("mentioned in issue other/proj#99", True))
        return httpx.Response(200, json=issue(1))

    assert build(handler).relations("1") == []


def test_relations_ignores_user_notes_that_look_like_system_notes():
    """利用者が同じ文面を書いても参照とはみなさない。"""

    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(403, json={})
        if "/notes" in str(request.url):
            return notes_response(("mentioned in issue #42", False))
        return httpx.Response(200, json=issue(1))

    assert build(handler).relations("1") == []


def test_relations_ignores_self_mention():
    def handler(request):
        if "/links" in str(request.url):
            return httpx.Response(403, json={})
        if "/notes" in str(request.url):
            return notes_response(("mentioned in issue #1", True))
        return httpx.Response(200, json=issue(1))

    assert build(handler).relations("1") == []
