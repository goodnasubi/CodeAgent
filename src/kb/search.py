"""ハイブリッド検索。3 つの信号を RRF でまとめる。

    類似度検索（pgvector）  → 順位リスト ┐
    キーワード検索（KB API）→ 順位リスト ├→ RRF でマージ（順位のみ使用）
    つながりの展開（辺）    → 順位リスト ┘

**バックエンドによって使える信号の数が違う。** キーワード検索もつながりも
持たない Re:lation では類似度検索の 1 本だけになるが、RRF は順位しか使わない
ので本数が減っても成立する。使えない信号は `supports_*` を見て飛ばす。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping
from uuid import UUID

from .backends.base import KnowledgeBase, KnowledgeBaseError
from .db.repository import ChunkRepository
from .embeddings import EmbeddingProvider
from .ranking import DEFAULT_K, rank_by_hops, reciprocal_rank_fusion

logger = logging.getLogger(__name__)


class _ProviderDefault:
    """「足切りの距離は provider に訊く」を表す番人。

    None は「足切りしない」という別の意味に既に使われているため、省略と
    区別できる第三の値が要る。
    """

    def __repr__(self) -> str:  # pragma: no cover - デバッグ表示のみ
        return "<provider default>"


PROVIDER_DEFAULT = _ProviderDefault()

SIGNAL_VECTOR = "vector"
SIGNAL_KEYWORD = "keyword"
SIGNAL_GRAPH = "graph"

DEFAULT_WEIGHTS: Mapping[str, float] = {
    SIGNAL_VECTOR: 1.0,
    SIGNAL_KEYWORD: 1.0,
    # つながりは「人が張った関連」で有用だが、直接ヒットより弱く効かせる。
    # ホップを増やす代わりにここで減衰させる方が、関連の薄い知識を
    # 引き込みにくい。
    SIGNAL_GRAPH: 0.5,
}


@dataclass(frozen=True)
class SearchResult:
    kb_issue_id: str
    title: str
    url: str | None
    score: float
    sources: tuple[str, ...]
    """どの検索が拾ったか。「なぜ出てきたか」を画面に出すために持つ。"""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchDiagnostics:
    """どの信号が実際に効いたか。効かなかった理由も残す。"""

    used: tuple[str, ...]
    skipped: Mapping[str, str]
    development_embedding: bool = False
    """開発用の embedding で検索した（＝結果に意味がない）。

    **0 件のときも含め、検索したなら必ず載せる。** 「何も見つからなかった」
    のか「そもそも意味を見ていない」のかは、利用者には見分けがつかない。
    """


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResult]
    diagnostics: SearchDiagnostics


class HybridSearch:
    def __init__(
        self,
        *,
        repository: ChunkRepository,
        embedder: EmbeddingProvider,
        backend: KnowledgeBase | None = None,
        k: int = DEFAULT_K,
        weights: Mapping[str, float] | None = None,
        hops: int = 1,
        max_distance: float | None | _ProviderDefault = PROVIDER_DEFAULT,
    ) -> None:
        """
        Args:
            max_distance: 足切りの距離。既定では **embedder が申告する値**を
                使う（モデルごとに適正値が違い、共通の定数を置けないため）。
                数値を渡せばそれで上書きし、None を渡すと足切りしない。
        """
        self._repo = repository
        self._embedder = embedder
        self._backend = backend
        self._k = k
        self._weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
        self._hops = hops
        self._max_distance: float | None = (
            embedder.max_distance
            if isinstance(max_distance, _ProviderDefault)
            else max_distance
        )

    def search(
        self,
        *,
        tenant_id: UUID,
        query: str,
        limit: int = 10,
        candidates_per_signal: int = 50,
        overfetch: int = 100,
    ) -> SearchResponse:
        """
        Args:
            limit: 最終的に返す件数の上限。
            candidates_per_signal: 各信号から集める候補の数。通常は返す件数
                より多めに取る。RRF は順位を突き合わせて評価するので、候補が
                少ないと「両方が拾った」という情報が得られない。
                ここを `limit` より小さくしても切り上げない（呼び出し側が
                指定した数を黙って増やさない）。結果が `limit` に満たなく
                なるだけ。
            overfetch: 類似度検索でチャンク単位に取る件数。
        """
        if not query.strip():
            return SearchResponse([], self._diagnostics((), {"query": "空のクエリ"}))
        candidates = candidates_per_signal

        lists: dict[str, list[str]] = {}
        skipped: dict[str, str] = {}
        fallback_titles: dict[str, tuple[str, str | None]] = {}

        # ---- 類似度検索（常に使える。pgvector 側で完結するため KB 障害に強い）
        hits = self._repo.search(
            tenant_id=tenant_id,
            query_embedding=self._embedder.embed([query])[0],
            model=self._embedder.model,
            limit=candidates,
            overfetch=max(overfetch, candidates),
            max_distance=self._max_distance,
        )
        vector_ids = [h.kb_issue_id for h in hits]
        if vector_ids:
            lists[SIGNAL_VECTOR] = vector_ids
        else:
            skipped[SIGNAL_VECTOR] = "十分に近い知識が無い"

        # ---- キーワード検索（KB 側。使えないバックエンドがある）
        if self._backend is None:
            skipped[SIGNAL_KEYWORD] = "バックエンド未設定"
        elif not self._backend.supports_keyword_search:
            skipped[SIGNAL_KEYWORD] = "このバックエンドは対応していない"
        else:
            try:
                found = self._backend.search(query, limit=candidates)
            except KnowledgeBaseError as exc:
                # KB が落ちていても類似度検索は返せる。縮退して続ける
                logger.warning("キーワード検索に失敗（縮退して続行）: %s", exc)
                skipped[SIGNAL_KEYWORD] = f"KB エラー: {exc}"
            else:
                if not found:
                    # 0 件でも理由を残す。ここを空けると「一致が無かった」と
                    # 「そもそも呼ばなかった」が診断上そっくりになる
                    skipped[SIGNAL_KEYWORD] = "一致する語が無い"
                else:
                    lists[SIGNAL_KEYWORD] = [k.id for k in found]
                    # まだ取り込んでいない知識がキーワード検索で出ることがある
                    # （取り込みの前に KB 側で作られた場合など）。表示が空欄に
                    # ならないよう、KB が返した題名を控えとして持っておく。
                    fallback_titles = {
                        k.id: (k.title, k.url or None) for k in found
                    }

        # ---- つながりの展開（上位ヒットから 1 ホップ）
        seeds = list(dict.fromkeys(vector_ids + lists.get(SIGNAL_KEYWORD, [])))
        if self._backend is not None and not self._backend.supports_relations:
            skipped[SIGNAL_GRAPH] = "このバックエンドは対応していない"
        elif not seeds:
            skipped[SIGNAL_GRAPH] = "起点となるヒットが無い"
        else:
            neighbours = self._repo.neighbours(
                tenant_id=tenant_id, kb_issue_ids=seeds, hops=self._hops
            )
            if neighbours:
                lists[SIGNAL_GRAPH] = rank_by_hops(neighbours)
            else:
                skipped[SIGNAL_GRAPH] = "つながりが無い"

        if not lists:
            return SearchResponse([], self._diagnostics((), skipped))

        fused = reciprocal_rank_fusion(
            lists, k=self._k, weights=self._weights, limit=limit
        )
        meta = self._repo.knowledge_meta(
            tenant_id=tenant_id, kb_issue_ids=[h.kb_issue_id for h in fused]
        )

        results = []
        for hit in fused:
            info = meta.get(hit.kb_issue_id)
            spare = fallback_titles.get(hit.kb_issue_id, ("", None))
            results.append(
                SearchResult(
                    kb_issue_id=hit.kb_issue_id,
                    title=(info.title if info else "") or spare[0],
                    url=(info.url if info else None) or spare[1],
                    score=hit.score,
                    sources=hit.sources,
                    labels=info.labels if info else (),
                )
            )

        return SearchResponse(results, self._diagnostics(tuple(lists), skipped))

    def _diagnostics(
        self, used: tuple[str, ...], skipped: Mapping[str, str]
    ) -> SearchDiagnostics:
        """どの経路で返るときも同じ診断を付ける。

        早期 return が 3 つあり、うち 2 つは「見つからなかった」経路。
        開発用 embedding の告知はそこでこそ要るので、組み立てを 1 箇所に寄せる。
        """
        return SearchDiagnostics(
            used=used,
            skipped=skipped,
            development_embedding=self._embedder.is_development,
        )
