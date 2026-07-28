"""知識どうしのつながり（辺テーブル）の統合テスト。"""

import uuid

import pytest

pytestmark = pytest.mark.integration


def test_relations_are_stored_in_both_directions(repo, tenant_id):
    """無向グラフとして扱うため、両向きの行が入る。"""
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")]
    )
    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"]) == {"B": 1}
    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["B"]) == {"A": 1}


def test_replacing_relations_removes_the_old_ones(repo, tenant_id):
    """KB 側で参照が外されたら、こちらのつながりも消える。"""
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")]
    )
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="A", related=[("C", "references")]
    )

    neighbours = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"])
    assert neighbours == {"C": 1}
    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["B"]) == {}


def test_empty_relations_clears_edges(repo, tenant_id):
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")]
    )
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="A", related=[])
    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"]) == {}


def test_starting_points_are_excluded_from_neighbours(repo, tenant_id):
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")]
    )
    result = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A", "B"])
    assert result == {}, "起点そのものが結果に混ざっている"


def test_one_hop_does_not_reach_two_hops_away(repo, tenant_id):
    """既定の 1 ホップでは、関連の薄い知識まで引き込まない。"""
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")])
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="B", related=[("C", "references")])

    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"], hops=1) == {"B": 1}
    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"], hops=2) == {
        "B": 1,
        "C": 2,
    }


def test_cycles_do_not_loop_forever(repo, tenant_id):
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")])
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="B", related=[("C", "references")])
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="C", related=[("A", "references")])

    result = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"], hops=5)
    assert set(result) == {"B", "C"}


def test_nearest_hop_wins_when_reachable_by_several_routes(repo, tenant_id):
    repo.replace_relations(
        tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references"), ("C", "references")]
    )
    repo.replace_relations(tenant_id=tenant_id, kb_issue_id="B", related=[("C", "references")])

    result = repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"], hops=3)
    assert result["C"] == 1, "遠回りの経路でホップ数が上書きされている"


def test_edges_do_not_leak_across_tenants(repo, tenant_id):
    other = uuid.uuid4()
    repo.ensure_tenant(other)
    try:
        repo.replace_relations(
            tenant_id=tenant_id, kb_issue_id="A", related=[("B", "references")]
        )
        assert repo.neighbours(tenant_id=other, kb_issue_ids=["A"]) == {}
    finally:
        repo._conn.execute(f'DROP TABLE IF EXISTS "ke_{other.hex}"')
        repo._conn.execute(f'DROP TABLE IF EXISTS "kc_{other.hex}"')


def test_no_starting_points_returns_empty(repo, tenant_id):
    assert repo.neighbours(tenant_id=tenant_id, kb_issue_ids=[]) == {}


def test_invalid_hops_rejected(repo, tenant_id):
    with pytest.raises(ValueError):
        repo.neighbours(tenant_id=tenant_id, kb_issue_ids=["A"], hops=0)
