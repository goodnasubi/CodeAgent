import pytest

from kb import chunking


def test_empty_input_yields_no_chunks():
    assert chunking.split("") == []
    assert chunking.split("   \n\n  ") == []


def test_short_text_is_one_chunk():
    chunks = chunking.split("プリンターが動かない。")
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert "プリンター" in chunks[0].text


def test_chunks_respect_max_chars():
    text = "\n\n".join(f"段落{i}。" * 30 for i in range(20))
    chunks = chunking.split(text, max_chars=300, overlap=20)
    assert len(chunks) > 1
    assert all(len(c.text) <= 300 for c in chunks)


def test_indexes_are_sequential():
    text = "\n\n".join(f"段落{i}。" * 40 for i in range(10))
    chunks = chunking.split(text, max_chars=200, overlap=0)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_overlap_carries_context_across_boundary():
    text = "\n\n".join(f"ブロック{i}" * 30 for i in range(6))
    with_overlap = chunking.split(text, max_chars=200, overlap=50)
    without = chunking.split(text, max_chars=200, overlap=0)
    # 重複がある分、合計文字数は増える
    assert sum(len(c.text) for c in with_overlap) > sum(len(c.text) for c in without)


def test_table_header_is_repeated_when_table_is_split():
    header = "| ID | 現象 | 対処 |\n| --- | --- | --- |"
    rows = "\n".join(f"| {i} | 現象{i}がでる | 対処{i}を実施 |" for i in range(40))
    chunks = chunking.split(f"{header}\n{rows}", max_chars=400, overlap=0)

    assert len(chunks) > 1, "この入力は分割されるはず"
    for c in chunks:
        assert "| ID | 現象 | 対処 |" in c.text, "ヘッダが失われると列の意味が分からなくなる"


def test_small_table_is_not_split():
    md = "| ID | 現象 |\n| --- | --- |\n| 1 | ORA-01555 |"
    chunks = chunking.split(md, max_chars=1500)
    assert len(chunks) == 1


def test_heading_starts_a_new_block():
    md = "# 見出しA\n本文A\n\n# 見出しB\n本文B"
    chunks = chunking.split(md, max_chars=1500)
    assert len(chunks) == 1
    assert "見出しA" in chunks[0].text and "見出しB" in chunks[0].text


def test_single_line_longer_than_max_is_force_split():
    chunks = chunking.split("あ" * 1000, max_chars=100, overlap=0)
    assert len(chunks) > 1
    assert all(len(c.text) <= 100 for c in chunks)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 0},
        {"max_chars": -1},
        {"overlap": -1},
        {"max_chars": 100, "overlap": 100},
        {"max_chars": 100, "overlap": 200},
    ],
)
def test_invalid_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        chunking.split("本文", **kwargs)
