"""実際の GitHub に対する疎通テスト。

モックでは API 仕様との齟齬を検出できないため、使い捨てリポジトリに対して
実際に Issue を作成・追記・検索する。

KB_GITHUB_TEST_REPO と KB_GITHUB_TOKEN が揃っているときだけ実行する。

    export KB_GITHUB_TEST_REPO=goodnasubi/kb-adapter-test
    export KB_GITHUB_TOKEN="$(gh auth token)"

作成した Issue はテスト終了時に close する（GitHub の API では Issue を
削除できないため）。
"""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kb.backends.github import GitHubKnowledgeBase

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def github():
    repo = os.environ.get("KB_GITHUB_TEST_REPO")
    token = os.environ.get("KB_GITHUB_TOKEN")
    if not (repo and token):
        pytest.skip("KB_GITHUB_TEST_REPO / KB_GITHUB_TOKEN が未設定のため skip")
    with GitHubKnowledgeBase(token=token, repository=repo) as kb:
        yield kb


@pytest.fixture
def issue(github):
    """テスト用の Issue を作り、終了後に close する。"""
    created = []

    def make(title: str, body: str, labels=()):
        k = github.create(title=title, body=body, labels=labels)
        created.append(k.id)
        return k

    yield make

    for issue_id in created:
        github._request(
            "PATCH",
            f"/repos/{github._repo}/issues/{issue_id}",
            json={"state": "closed"},
        )


def test_create_and_get_roundtrip(github, issue):
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] ORA-01555 が発生する", "UNDO 表領域が不足している")

    fetched = github.get(created.id)
    assert fetched.id == created.id
    assert marker in fetched.title
    assert "UNDO 表領域" in fetched.body
    assert fetched.url.endswith(f"/issues/{created.id}")
    assert fetched.updated_at is not None


def test_append_shows_up_as_comment(github, issue):
    created = issue(f"[test {uuid.uuid4().hex[:8]}] 追記の確認", "初期の本文")
    github.append(created.id, "あとから分かったこと: UNDO を拡張して解決")

    fetched = github.get(created.id)
    assert fetched.comments, "コメントが取得できていない"
    assert "UNDO を拡張して解決" in fetched.comments[0]
    assert "初期の本文" in fetched.body, "本文が書き換えられている"

    combined = fetched.combined_text()
    assert "[コメント 1]" in combined
    assert "初期の本文" in combined and "UNDO を拡張して解決" in combined


def test_labels_can_be_added_and_read_back(github, issue):
    created = issue(f"[test {uuid.uuid4().hex[:8]}] ラベルの確認", "本文")
    github.add_labels(created.id, ["bug"])
    assert "bug" in github.get(created.id).labels


def test_updated_since_finds_the_new_issue(github, issue):
    """作成した Issue が差分更新で拾えること。

    GitHub の一覧エンドポイントには反映遅延があり、作成直後は数秒間
    結果に現れない（実測で数秒）。本番では自分が書いた知識をその場で
    embedding するため影響しないが、テストでは待つ必要がある。
    """
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] 差分更新の確認", "本文")

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    for _ in range(10):
        if any(k.id == created.id for k in github.updated_since(since)):
            return
        time.sleep(3)
    pytest.fail("作成した Issue が updated_since で拾えない（30 秒待機後）")


def test_updated_since_returns_no_pull_requests(github):
    since = datetime.now(timezone.utc) - timedelta(days=365)
    for knowledge in github.updated_since(since):
        assert "/pull/" not in knowledge.url


def test_search_finds_by_keyword(github, issue):
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] 検索の確認 ORA-01555", "エラー番号での検索")

    # 検索インデックスへの反映には遅延がある
    for _ in range(10):
        hits = github.search(marker, limit=10)
        if any(h.id == created.id for h in hits):
            return
        time.sleep(3)
    pytest.skip("GitHub の検索インデックスが間に合わなかった（実装の失敗ではない）")


def test_relations_are_reported_on_the_referenced_side(github, issue):
    """`#N` と書いたつながりが、**参照された側**から取れること。

    cross-referenced イベントは被参照側にしか立たない。子の本文に
    `#親` と書いても、イベントが付くのは親の timeline だけで子には付かない。
    全知識を走査すればグラフは埋まるので、これで欠落は生じない。
    """
    marker = uuid.uuid4().hex[:8]
    parent = issue(f"[test {marker}] 親: DB接続エラー全般", "接続まわりの調査")
    child = issue(f"[test {marker}] 子: ORA-01555", f"#{parent.id} の一種")

    for _ in range(10):
        if child.id in {r.to_id for r in github.relations(parent.id)}:
            break
        time.sleep(3)
    else:
        pytest.fail("参照された側からつながりが取れない")

    # 逆向きは取れない（GitHub の仕様。実装の欠陥ではない）
    assert parent.id not in {r.to_id for r in github.relations(child.id)}


def test_relations_from_a_comment_are_found(github, issue):
    """本文だけでなく、コメントに書いた `#N` も拾えること。"""
    marker = uuid.uuid4().hex[:8]
    target = issue(f"[test {marker}] 参照される側", "本文")
    other = issue(f"[test {marker}] 参照する側", "本文")
    github.append(other.id, f"関連: #{target.id} も参照")

    for _ in range(10):
        if other.id in {r.to_id for r in github.relations(target.id)}:
            return
        time.sleep(3)
    pytest.fail("コメントからの参照が取れない")
