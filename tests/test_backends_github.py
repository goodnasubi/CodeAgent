"""GitHub アダプタの単体テスト。HTTP はモックする。"""

from datetime import datetime, timezone

import httpx
import pytest

from kb.backends import Knowledge, KnowledgeBase, KnowledgeBaseError, KnowledgeNotFound
from kb.backends.github import GitHubKnowledgeBase

REPO = "acme/kb"


def build(handler) -> GitHubKnowledgeBase:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    return GitHubKnowledgeBase(token="t", repository=REPO, client=client)


def issue(number=1, *, title="題名", body="本文", labels=(), comments=0, pr=False):
    payload = {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "labels": [{"name": n} for n in labels],
        "updated_at": "2026-07-29T01:02:03Z",
        "comments": comments,
    }
    if pr:
        payload["pull_request"] = {"url": "..."}
    return payload


def test_satisfies_protocol():
    assert isinstance(build(lambda r: httpx.Response(200, json=[])), KnowledgeBase)


def test_repository_format_is_validated():
    with pytest.raises(ValueError):
        GitHubKnowledgeBase(token="t", repository="name-only")


def test_create_posts_issue():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=issue(7, title="新しい知識"))

    kb = build(handler)
    result = kb.create(title="新しい知識", body="内容", labels=["bug"])

    assert seen["method"] == "POST"
    assert seen["url"].endswith(f"/repos/{REPO}/issues")
    assert seen["body"] == {"title": "新しい知識", "body": "内容", "labels": ["bug"]}
    assert result.id == "7"
    assert result.title == "新しい知識"


def test_get_collects_comments():
    def handler(request):
        if "/comments" in str(request.url):
            return httpx.Response(200, json=[{"body": "追記1"}, {"body": "追記2"}])
        return httpx.Response(200, json=issue(3, comments=2))

    k = build(handler).get("3")
    assert k.comments == ("追記1", "追記2")


def test_get_skips_comment_request_when_there_are_none():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=issue(3, comments=0))

    build(handler).get("3")
    assert not any("/comments" in c for c in calls), "不要なリクエストを送っている"


def test_get_rejects_pull_request():
    kb = build(lambda r: httpx.Response(200, json=issue(9, pr=True)))
    with pytest.raises(KnowledgeNotFound):
        kb.get("9")


def test_append_adds_comment_not_body_edit():
    seen = {}

    def handler(request):
        import json

        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={})

    build(handler).append("5", "あとから分かったこと")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/issues/5/comments")
    assert seen["body"] == {"body": "あとから分かったこと"}


def test_append_rejects_empty_text():
    kb = build(lambda r: httpx.Response(201, json={}))
    with pytest.raises(ValueError):
        kb.append("5", "   ")


def test_add_labels_skips_request_when_empty():
    calls = []
    kb = build(lambda r: (calls.append(1), httpx.Response(200, json=[]))[1])
    kb.add_labels("5", [])
    assert calls == []


def test_updated_since_excludes_pull_requests():
    def handler(request):
        if "/comments" in str(request.url):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[issue(1), issue(2, pr=True), issue(3)])

    kb = build(handler)
    ids = [k.id for k in kb.updated_since(datetime(2026, 7, 1, tzinfo=timezone.utc))]
    assert ids == ["1", "3"], "PR が知識として取り込まれている"


def test_updated_since_follows_pagination():
    pages = {
        "https://api.github.com/repos/acme/kb/issues": (
            [issue(1)],
            '<https://api.github.com/page2>; rel="next"',
        ),
        "https://api.github.com/page2": ([issue(2)], None),
    }

    def handler(request):
        url = str(request.url).split("?")[0]
        body, link = pages[url]
        headers = {"Link": link} if link else {}
        return httpx.Response(200, json=body, headers=headers)

    kb = build(handler)
    ids = [k.id for k in kb.updated_since(datetime(2026, 7, 1, tzinfo=timezone.utc))]
    assert ids == ["1", "2"]


def test_updated_since_sends_utc_timestamp():
    seen = {}

    def handler(request):
        seen["since"] = dict(request.url.params).get("since")
        return httpx.Response(200, json=[])

    kb = build(handler)
    list(kb.updated_since(datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)))
    assert seen["since"] == "2026-07-29T12:00:00Z"


def test_search_scopes_query_to_repository():
    seen = {}

    def handler(request):
        seen["q"] = dict(request.url.params).get("q")
        return httpx.Response(200, json={"items": [issue(1), issue(2)]})

    results = build(handler).search("ORA-01555", limit=5)
    assert seen["q"] == f"repo:{REPO} is:issue ORA-01555"
    assert [k.id for k in results] == ["1", "2"]


def test_search_respects_limit():
    kb = build(lambda r: httpx.Response(200, json={"items": [issue(i) for i in range(20)]}))
    assert len(kb.search("x", limit=3)) == 3


def test_rate_limit_is_reported_clearly():
    kb = build(
        lambda r: httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999"}, json={}
        )
    )
    with pytest.raises(KnowledgeBaseError, match="レート制限"):
        kb.get("1")


def test_missing_issue_raises_not_found():
    kb = build(lambda r: httpx.Response(404, json={}))
    with pytest.raises(KnowledgeNotFound):
        kb.get("404")


def test_connection_error_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(KnowledgeBaseError, match="接続に失敗"):
        build(handler).get("1")


# ------------------------------------------------------------ combined_text


def test_combined_text_includes_all_parts_with_markers():
    k = Knowledge(
        id="1",
        title="ORA-01555 が出る",
        body="UNDO 表領域が不足している",
        url="",
        comments=("拡張して解決した",),
        attachments=(("log.xlsx", "エラーログの内容"),),
    )
    text = k.combined_text()
    assert "ORA-01555 が出る" in text
    assert "UNDO 表領域が不足している" in text
    assert "[コメント 1]" in text and "拡張して解決した" in text
    assert "[添付 log.xlsx]" in text and "エラーログの内容" in text


def test_combined_text_skips_empty_sections():
    k = Knowledge(id="1", title="題名", body="", url="", comments=("", "  "))
    assert k.combined_text() == "題名"


# ------------------------------------------------------------- relations


def timeline_event(number, *, pr=False, kind="cross-referenced"):
    src = {"number": number, "title": f"関連 #{number}"}
    if pr:
        src["pull_request"] = {"url": "..."}
    return {"event": kind, "source": {"issue": src}}


def test_relations_collects_cross_references():
    kb = build(
        lambda r: httpx.Response(
            200, json=[timeline_event(2), {"event": "commented"}, timeline_event(3)]
        )
    )
    rels = kb.relations("1")
    assert {r.to_id for r in rels} == {"2", "3"}
    assert all(r.from_id == "1" for r in rels)


def test_relations_excludes_pull_requests():
    kb = build(lambda r: httpx.Response(200, json=[timeline_event(2), timeline_event(9, pr=True)]))
    assert {r.to_id for r in kb.relations("1")} == {"2"}, "PR が関係グラフに混入している"


def test_relations_ignores_self_reference():
    kb = build(lambda r: httpx.Response(200, json=[timeline_event(1), timeline_event(2)]))
    assert {r.to_id for r in kb.relations("1")} == {"2"}


def test_relations_deduplicates_repeated_references():
    """同じ Issue を何度参照しても、つながりは 1 本。"""
    kb = build(lambda r: httpx.Response(200, json=[timeline_event(2)] * 3))
    assert len(kb.relations("1")) == 1


def test_relations_empty_when_no_cross_references():
    kb = build(lambda r: httpx.Response(200, json=[{"event": "labeled"}]))
    assert kb.relations("1") == []
