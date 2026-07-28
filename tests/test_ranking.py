import pytest

from kb.ranking import (
    DEFAULT_K,
    dedupe_preserving_order,
    rank_by_hops,
    reciprocal_rank_fusion as rrf,
)


def ids(hits):
    return [h.kb_issue_id for h in hits]


def test_empty_input():
    assert rrf({}) == []
    assert rrf({"vector": []}) == []


def test_single_list_preserves_order():
    assert ids(rrf({"vector": ["a", "b", "c"]})) == ["a", "b", "c"]


def test_appearing_in_two_lists_beats_appearing_in_one():
    """両方の検索で拾われたものが上に来る。RRF の要点。"""
    result = rrf({"vector": ["a", "b"], "keyword": ["b", "c"]})
    assert result[0].kb_issue_id == "b"
    assert set(result[0].sources) == {"vector", "keyword"}


def test_top_of_one_list_can_beat_middle_of_two():
    """1 位を独占する強さと、2 リストに載る強さの比較が k で決まる。"""
    # k が小さいと上位の順位が強く効く
    small_k = rrf({"vector": ["a"] + [f"x{i}" for i in range(20)],
                   "keyword": ["b"] + [f"y{i}" for i in range(20)]}, k=1)
    assert small_k[0].kb_issue_id in {"a", "b"}


def test_sources_records_which_search_found_it():
    result = rrf({"vector": ["a"], "keyword": ["a"], "graph": ["b"]})
    by_id = {h.kb_issue_id: h for h in result}
    assert set(by_id["a"].sources) == {"vector", "keyword"}
    assert by_id["b"].sources == ("graph",)


def test_ranks_are_recorded_per_search():
    result = rrf({"vector": ["a", "b"], "keyword": ["b", "a"]})
    by_id = {h.kb_issue_id: h for h in result}
    assert by_id["a"].ranks == {"vector": 1, "keyword": 2}
    assert by_id["b"].ranks == {"vector": 2, "keyword": 1}


def test_weights_can_damp_a_signal():
    """グラフ展開を弱く効かせられること。

    重みは順位から得た寄与に掛かるので、比較不能なスコアを混ぜる問題は
    起きない。
    """
    strong = rrf({"vector": ["a"], "graph": ["b"]})
    damped = rrf({"vector": ["a"], "graph": ["b"]}, weights={"graph": 0.1})

    assert {h.kb_issue_id: h.score for h in strong}["a"] == pytest.approx(
        {h.kb_issue_id: h.score for h in strong}["b"]
    )
    scores = {h.kb_issue_id: h.score for h in damped}
    assert scores["a"] > scores["b"], "重みを下げた検索の寄与が減っていない"


def test_duplicate_within_one_list_counts_once():
    once = rrf({"vector": ["a", "b"]})
    twice = rrf({"vector": ["a", "a", "b"]})
    assert {h.kb_issue_id: h.score for h in once}["a"] == pytest.approx(
        {h.kb_issue_id: h.score for h in twice}["a"]
    )


def test_limit_truncates():
    assert len(rrf({"vector": list("abcdef")}, limit=3)) == 3


def test_ties_are_ordered_stably():
    a = ids(rrf({"vector": ["x"], "keyword": ["y"]}))
    b = ids(rrf({"keyword": ["y"], "vector": ["x"]}))
    assert a == b == ["x", "y"]


def test_invalid_k_rejected():
    with pytest.raises(ValueError):
        rrf({"vector": ["a"]}, k=0)


def test_default_k_is_the_documented_value():
    assert DEFAULT_K == 60


# --------------------------------------------------------------- graph側


def test_rank_by_hops_orders_nearest_first():
    # 同じホップ数の中は ID 順（つながりの強さを測る指標が無いため）
    assert rank_by_hops({"zulu": 2, "near": 1, "alpha": 2}) == ["near", "alpha", "zulu"]


def test_rank_by_hops_is_stable_within_a_hop():
    assert rank_by_hops({"b": 1, "a": 1}) == ["a", "b"]


def test_rank_by_hops_empty():
    assert rank_by_hops({}) == []


def test_dedupe_preserves_first_occurrence():
    assert dedupe_preserving_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
