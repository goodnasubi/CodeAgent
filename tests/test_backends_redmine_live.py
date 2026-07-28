"""実際の Redmine に対する疎通テスト。

Docker で立てた Redmine に対して実行する。立て方は verify/README.md を参照。

    export KB_REDMINE_URL=http://localhost:3000
    export KB_REDMINE_API_KEY=...
    export KB_REDMINE_PROJECT=kb-adapter-test
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kb.backends.redmine import RedmineKnowledgeBase

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redmine():
    url = os.environ.get("KB_REDMINE_URL")
    key = os.environ.get("KB_REDMINE_API_KEY")
    project = os.environ.get("KB_REDMINE_PROJECT")
    if not (url and key and project):
        pytest.skip("KB_REDMINE_URL / API_KEY / PROJECT が未設定のため skip")
    with RedmineKnowledgeBase(api_key=key, base_url=url, project=project) as kb:
        yield kb


@pytest.fixture
def issue(redmine):
    created = []

    def make(title: str, body: str, labels=()):
        k = redmine.create(title=title, body=body, labels=labels)
        created.append(k.id)
        return k

    yield make

    for issue_id in created:
        # 5 = Closed（既定データ）
        redmine._request(
            "PUT", f"/issues/{issue_id}.json", json={"issue": {"status_id": 5}}
        )


def test_create_and_get_roundtrip(redmine, issue):
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] ORA-01555 が発生する", "UNDO 表領域が不足している")

    fetched = redmine.get(created.id)
    assert fetched.id == created.id
    assert marker in fetched.title
    assert "UNDO 表領域" in fetched.body
    assert fetched.url.endswith(f"/issues/{created.id}")
    assert fetched.updated_at is not None


def test_append_shows_up_as_note(redmine, issue):
    created = issue(f"[test {uuid.uuid4().hex[:8]}] 追記の確認", "初期の説明")
    redmine.append(created.id, "あとから分かったこと: UNDO を拡張して解決")

    fetched = redmine.get(created.id)
    assert any("UNDO を拡張して解決" in c for c in fetched.comments)
    assert "初期の説明" in fetched.body, "説明欄が書き換えられている"


def test_property_changes_are_not_treated_as_notes(redmine, issue):
    """優先度変更などの属性変更だけの journal を知識に混ぜない。"""
    created = issue(f"[test {uuid.uuid4().hex[:8]}] 属性変更の確認", "本文")
    redmine.append(created.id, "人が書いた追記")
    # 注記を伴わない属性変更（journal は作られるが notes は空）
    redmine._request(
        "PUT", f"/issues/{created.id}.json", json={"issue": {"priority_id": 1}}
    )

    comments = redmine.get(created.id).comments
    assert comments == ("人が書いた追記",), f"属性変更が混入している: {comments}"


def test_labels_via_custom_field(redmine, issue):
    """Redmine にラベル機能は無いため、複数選択カスタムフィールドで代用する。"""
    created = issue(f"[test {uuid.uuid4().hex[:8]}] ラベルの確認", "本文", labels=["bug"])
    assert "bug" in redmine.get(created.id).labels

    redmine.add_labels(created.id, ["db"])
    labels = set(redmine.get(created.id).labels)
    assert {"bug", "db"} <= labels, f"既存ラベルが消えている: {labels}"


def test_updated_since_includes_closed_issues(redmine, issue):
    """解決済みの知識こそ検索対象。status_id=* が無いと取りこぼす。"""
    created = issue(f"[test {uuid.uuid4().hex[:8]}] 完了済みの取り込み", "本文")
    redmine._request(
        "PUT", f"/issues/{created.id}.json", json={"issue": {"status_id": 5}}
    )

    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    found = [k.id for k in redmine.updated_since(since)]
    assert created.id in found, "完了済みチケットが差分更新で拾えていない"


def test_search_finds_by_keyword(redmine, issue):
    marker = uuid.uuid4().hex[:8]
    created = issue(f"[test {marker}] 検索の確認", f"固有語 {marker} を含む説明")

    hits = redmine.search(marker, limit=10)
    assert any(h.id == created.id for h in hits), "キーワード検索で見つからない"


def test_relations_are_readable_from_both_sides(redmine, issue):
    """Redmine の関連チケットは 1 レコードで双方向を表す。"""
    marker = uuid.uuid4().hex[:8]
    parent = issue(f"[test {marker}] 親: DB接続エラー全般", "接続まわりの調査")
    child = issue(f"[test {marker}] 子: ORA-01555", "UNDO 不足が原因")

    redmine._request(
        "POST",
        f"/issues/{child.id}/relations.json",
        json={"relation": {"issue_to_id": int(parent.id), "relation_type": "relates"}},
    )

    assert parent.id in {r.to_id for r in redmine.relations(child.id)}
    assert child.id in {r.to_id for r in redmine.relations(parent.id)}, (
        "関連が片側からしか見えない"
    )


def test_missing_issue_raises_not_found(redmine):
    from kb.backends import KnowledgeNotFound

    with pytest.raises(KnowledgeNotFound):
        redmine.get("999999")
