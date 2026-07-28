"""実際の GitLab に対する疎通テスト。

モックでは API 仕様との齟齬を検出できないため、使い捨てプロジェクトに対して
実際に Issue を作成・追記・検索する。

KB_GITLAB_TEST_PROJECT と KB_GITLAB_TOKEN が揃っているときだけ実行する。

    export KB_GITLAB_TEST_PROJECT=synapse-corporation-group/kb-adapter-test
    export KB_GITLAB_TOKEN="$(cat ~/.config/kb-dev/gitlab-token)"

self-hosted を試す場合は KB_GITLAB_API_BASE も指定する。

作成した Issue はテスト終了時に close する。
"""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kb.backends.gitlab import GitLabKnowledgeBase

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def gitlab():
    project = os.environ.get("KB_GITLAB_TEST_PROJECT")
    token = os.environ.get("KB_GITLAB_TOKEN")
    if not (project and token):
        pytest.skip("KB_GITLAB_TEST_PROJECT / KB_GITLAB_TOKEN が未設定のため skip")
    kwargs = {}
    if os.environ.get("KB_GITLAB_API_BASE"):
        kwargs["api_base"] = os.environ["KB_GITLAB_API_BASE"]
    with GitLabKnowledgeBase(token=token, project=project, **kwargs) as kb:
        yield kb


@pytest.fixture
def issue(gitlab):
    created = []

    def make(title: str, body: str, labels=()):
        k = gitlab.create(title=title, body=body, labels=labels)
        created.append(k.id)
        return k

    yield make

    for issue_id in created:
        gitlab._request(
            "PUT",
            f"/projects/{gitlab._project}/issues/{issue_id}",
            json={"state_event": "close"},
        )


def test_create_and_get_roundtrip(gitlab, issue):
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] ORA-01555 が発生する", "UNDO 表領域が不足している")

    fetched = gitlab.get(created.id)
    assert fetched.id == created.id
    assert marker in fetched.title
    assert "UNDO 表領域" in fetched.body
    # GitLab は Issue を Work Items へ移行中で、web_url は /-/issues/N ではなく
    # /-/work_items/N を返す。経路を決め打ちせず、番号が一致することだけ見る。
    assert fetched.url.rstrip("/").endswith(created.id)
    assert fetched.updated_at is not None


def test_append_shows_up_as_note(gitlab, issue):
    created = issue(f"[test {uuid.uuid4().hex[:8]}] 追記の確認", "初期の本文")
    gitlab.append(created.id, "あとから分かったこと: UNDO を拡張して解決")

    fetched = gitlab.get(created.id)
    assert fetched.comments, "ノートが取得できていない"
    assert any("UNDO を拡張して解決" in c for c in fetched.comments)
    assert "初期の本文" in fetched.body, "本文が書き換えられている"


def test_system_notes_are_excluded(gitlab, issue):
    """ラベル追加は system note を生む。知識に混ざってはいけない。"""
    created = issue(f"[test {uuid.uuid4().hex[:8]}] システムノートの確認", "本文")
    gitlab.add_labels(created.id, ["kb-test"])
    gitlab.append(created.id, "人が書いた追記")

    comments = gitlab.get(created.id).comments
    assert any("人が書いた追記" in c for c in comments)
    assert not any("label" in c.lower() for c in comments), (
        f"システムノートが混入している: {comments}"
    )


def test_labels_are_added_without_replacing(gitlab, issue):
    created = issue(f"[test {uuid.uuid4().hex[:8]}] ラベルの確認", "本文", labels=["kb-test"])
    gitlab.add_labels(created.id, ["kb-second"])

    labels = set(gitlab.get(created.id).labels)
    assert {"kb-test", "kb-second"} <= labels, f"既存ラベルが消えている: {labels}"


def test_updated_since_finds_the_new_issue(gitlab, issue):
    created = issue(f"[test {uuid.uuid4().hex[:8]}] 差分更新の確認", "本文")
    since = datetime.now(timezone.utc) - timedelta(minutes=5)

    for _ in range(10):
        if any(k.id == created.id for k in gitlab.updated_since(since)):
            return
        time.sleep(3)
    pytest.fail("作成した Issue が updated_since で拾えない")


def test_search_finds_by_keyword(gitlab, issue):
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] 検索の確認 ORA-01555", "エラー番号での検索")

    for _ in range(10):
        if any(h.id == created.id for h in gitlab.search(marker, limit=10)):
            return
        time.sleep(3)
    pytest.skip("GitLab の検索インデックスが間に合わなかった（実装の失敗ではない）")


def test_related_issues_are_readable_from_both_sides(gitlab, issue):
    """GitLab の関連イシューは双方向に張られる。

    GitHub の参照イベントと違い、どちら側から引いても相手が返る。
    """
    marker = uuid.uuid4().hex[:8]
    parent = issue(f"[test {marker}] 親: DB接続エラー全般", "接続まわりの調査")
    child = issue(f"[test {marker}] 子: ORA-01555", "UNDO 不足が原因")

    # リンク作成の target_project_id は**数値の ID** でなければ 404 になる
    # （URL エンコード済みパスは受け付けられない）。読み取り側には影響しない。
    project_id = gitlab._request("GET", f"/projects/{gitlab._project}").json()["id"]
    gitlab._request(
        "POST",
        f"/projects/{gitlab._project}/issues/{child.id}/links",
        json={"target_project_id": project_id, "target_issue_iid": parent.id},
    )

    assert parent.id in {r.to_id for r in gitlab.relations(child.id)}
    assert child.id in {r.to_id for r in gitlab.relations(parent.id)}, (
        "GitLab の関連イシューが片側からしか見えない"
    )


def test_plain_reference_is_found_without_the_links_api(gitlab, issue):
    """`#N` と書くだけのつながりが拾えること。

    関連イシュー API は Premium 機能のため、Free 版や古い self-hosted では
    使えない。その環境でも、GitLab が自動で残すシステムノート
    （`mentioned in issue #N`）から参照を拾える必要がある。
    """
    marker = uuid.uuid4().hex[:8]
    target = issue(f"[test {marker}] 被参照側", "本文")
    # 関連イシュー機能を使わず、本文に書くだけ
    source = issue(f"[test {marker}] 参照する側", f"#{target.id} の一種である")

    for _ in range(10):
        if source.id in {r.to_id for r in gitlab.relations(target.id)}:
            return
        time.sleep(3)
    pytest.fail("システムノート経由で参照を拾えていない")


def test_relations_survive_without_premium_links_api(gitlab, issue, monkeypatch):
    """links API が 403 を返す環境を模して、機能が落ちないことを確かめる。"""
    marker = uuid.uuid4().hex[:8]
    target = issue(f"[test {marker}] Free版想定・被参照", "本文")
    source = issue(f"[test {marker}] Free版想定・参照元", f"#{target.id} を参照")

    original = gitlab._request

    def deny_links(method, path, **kwargs):
        if "/links" in path:
            return None  # tolerate=(403, 404) が返す値と同じ
        return original(method, path, **kwargs)

    for _ in range(10):
        monkeypatch.setattr(gitlab, "_request", deny_links)
        found = {r.to_id for r in gitlab.relations(target.id)}
        monkeypatch.undo()
        if source.id in found:
            return
        time.sleep(3)
    pytest.fail("links API 無しではつながりを拾えていない")
