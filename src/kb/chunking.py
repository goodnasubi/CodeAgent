"""Markdown テキストのチャンク分割。

embedding にはトークン上限があるため、長い文書は分割して格納する。
分割は文書の構造（見出し・段落・表）を尊重し、意味の切れ目でない箇所で
断ち切らないようにする。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 見出し行（# ... ###### ...）
_HEADING = re.compile(r"^#{1,6} ")
# 表の行（| a | b | の形）
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
# 表の区切り行（| --- | --- |）
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str


def _blocks(markdown: str) -> list[str]:
    """Markdown を意味のまとまり（ブロック）に分ける。

    表は複数行で 1 つの意味を成すため、途中で切らずに 1 ブロックとして扱う。
    """
    blocks: list[str] = []
    buf: list[str] = []
    in_table = False

    def flush() -> None:
        nonlocal buf
        if buf:
            text = "\n".join(buf).strip()
            if text:
                blocks.append(text)
            buf = []

    for line in markdown.splitlines():
        is_table_line = bool(_TABLE_ROW.match(line))

        if in_table and not is_table_line:
            flush()
            in_table = False

        if _HEADING.match(line):
            flush()
            buf.append(line)
            flush()
            continue

        if is_table_line and not in_table:
            flush()
            in_table = True

        if not line.strip() and not in_table:
            flush()
            continue

        buf.append(line)

    flush()
    return blocks


def _table_header(block: str) -> str | None:
    """表ブロックのヘッダ（見出し行 + 区切り行）を返す。表でなければ None。"""
    lines = block.splitlines()
    if len(lines) >= 2 and _TABLE_ROW.match(lines[0]) and _TABLE_SEP.match(lines[1]):
        return "\n".join(lines[:2])
    return None


def _split_oversized(block: str, max_chars: int) -> list[str]:
    """単体で上限を超えるブロックを分割する。

    表を割る場合はヘッダ行を各断片の先頭に繰り返す。ヘッダを失うと
    「どの列が何なのか」が分からなくなり、検索精度に直接響くため。
    """
    header = _table_header(block)
    lines = block.splitlines()
    body = lines[2:] if header else lines
    prefix = f"{header}\n" if header else ""

    parts: list[str] = []
    buf: list[str] = []
    size = len(prefix)

    for line in body:
        # 1 行だけで上限を超える場合は、文字数で強制的に切る
        if len(prefix) + len(line) > max_chars:
            if buf:
                parts.append(prefix + "\n".join(buf))
                buf, size = [], len(prefix)
            room = max_chars - len(prefix)
            for i in range(0, len(line), room):
                parts.append(prefix + line[i : i + room])
            continue

        if size + len(line) + 1 > max_chars and buf:
            parts.append(prefix + "\n".join(buf))
            buf, size = [], len(prefix)

        buf.append(line)
        size += len(line) + 1

    if buf:
        parts.append(prefix + "\n".join(buf))
    return parts or [block]


def split(markdown: str, *, max_chars: int = 1500, overlap: int = 150) -> list[Chunk]:
    """Markdown をチャンクに分割する。

    Args:
        max_chars: 1 チャンクの最大文字数。embedding のトークン上限に合わせる。
            日本語は 1 文字がおよそ 1 トークンになるため、文字数で近似する。
        overlap: 隣接チャンク間で重複させる文字数。境界にまたがる記述が
            どちらのチャンクからも見つからなくなるのを防ぐ。

    Returns:
        チャンクのリスト。入力が空なら空リスト。
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0:
        raise ValueError("overlap must not be negative")
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")

    text = markdown.strip()
    if not text:
        return []

    pieces: list[str] = []
    for block in _blocks(text):
        if len(block) > max_chars:
            pieces.extend(_split_oversized(block, max_chars))
        else:
            pieces.append(block)

    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    for piece in pieces:
        if buf and size + len(piece) + 2 > max_chars:
            chunks.append("\n\n".join(buf))
            tail = chunks[-1][-overlap:] if overlap else ""
            buf = [tail] if tail else []
            size = len(tail)
        buf.append(piece)
        size += len(piece) + 2

    if buf:
        joined = "\n\n".join(buf).strip()
        if joined:
            chunks.append(joined)

    return [Chunk(index=i, text=c) for i, c in enumerate(chunks)]
