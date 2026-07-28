"""検索結果のマージ（RRF: Reciprocal Rank Fusion）。

順位だけを使い、スコアは一切見ない。理由は、混ぜたい信号が互いに
比較できないため:

- 類似度検索が返すのはコサイン距離（連続値）
- キーワード検索が返すのは KB API の関連度。**そもそもスコアを返さない
  バックエンドがあり**、返す場合も 3 バックエンドで定義が揃わない
- グラフ展開が返すのはホップ数（離散値）

順位ならどれからも必ず得られる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

DEFAULT_K = 60
"""RRF の定数。慣例値。小さいほど上位の順位が強く効く。"""


@dataclass(frozen=True)
class FusedHit:
    kb_issue_id: str
    score: float
    sources: tuple[str, ...]
    """この知識を拾った検索の名前。「なぜ出てきたか」を UI で示すために持つ。"""
    ranks: Mapping[str, int] = field(default_factory=dict)
    """検索名 → その中での順位（1 始まり）。"""


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[str]],
    *,
    k: int = DEFAULT_K,
    weights: Mapping[str, float] | None = None,
    limit: int | None = None,
) -> list[FusedHit]:
    """順位リストを RRF でマージする。

    Args:
        ranked_lists: 検索名 → 知識 ID の並び（上位から）。
        k: RRF の定数。
        weights: 検索ごとの重み。**スコアの重み付けではなく、順位から
            得た寄与への重み**なので、比較不能なスコアを混ぜる問題は起きない。
            グラフ展開を弱めに効かせる、といった調整に使う。
        limit: 返す件数。

    Returns:
        スコアの高い順。同点は ID で安定に並べる。
    """
    if k <= 0:
        raise ValueError("k must be positive")

    scores: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    ranks: dict[str, dict[str, int]] = {}

    for name, ids in ranked_lists.items():
        weight = (weights or {}).get(name, 1.0)
        for position, issue_id in enumerate(ids, start=1):
            if issue_id in ranks.get(name, {}):
                continue  # 同一リスト内の重複は最上位のみ採用
            scores[issue_id] = scores.get(issue_id, 0.0) + weight / (k + position)
            sources.setdefault(issue_id, []).append(name)
            ranks.setdefault(name, {})[issue_id] = position

    per_issue_ranks: dict[str, dict[str, int]] = {}
    for name, mapping in ranks.items():
        for issue_id, position in mapping.items():
            per_issue_ranks.setdefault(issue_id, {})[name] = position

    fused = [
        FusedHit(
            kb_issue_id=issue_id,
            score=score,
            sources=tuple(sources[issue_id]),
            ranks=per_issue_ranks.get(issue_id, {}),
        )
        for issue_id, score in scores.items()
    ]
    fused.sort(key=lambda h: (-h.score, h.kb_issue_id))
    return fused[:limit] if limit is not None else fused


def rank_by_hops(neighbours: Mapping[str, int]) -> list[str]:
    """グラフ展開の結果を順位リストに変換する。

    近いものほど上位。同じホップ数の中は ID で安定に並べる（つながりの
    強さを測る指標が無いため、恣意的な順位付けをしない）。
    """
    return [
        issue_id
        for issue_id, _ in sorted(neighbours.items(), key=lambda kv: (kv[1], kv[0]))
    ]


def dedupe_preserving_order(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
