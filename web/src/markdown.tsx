/** KB の本文を描くための、ごく小さい Markdown 実装。
 *
 * **ライブラリは入れない。** 必要なのは GitHub の Issue 本文と、LLM の
 * 文字起こしが出す範囲（見出し・箇条書き・番号付き・コード・引用・水平線・
 * 強調・リンク）だけで、そのために依存を 1 つ増やすほどではない。
 *
 * **HTML 文字列は作らない。** 本文は外部から来る文字列で、生成 AI の出力も
 * 混ざる。React 要素として組み立てれば、エスケープは React が行うので
 * innerHTML 経由の混入経路がそもそも無い。
 *
 * **入れ子のリストと表はツリーで描く。** 中央の欄は幅が狭く、素の表は列が
 * 潰れて読めない。表は 1 列目をノード、残りの列を「見出し: 値」の子として
 * 開くことで、幅に依存せず読める形にしている。
 *
 * 対応していないもの（意図的）:
 * - **斜体。** `_` や `*` の囲みは `UNDO_RETENTION` や `SELECT *` のような
 *   技術的な文字列を壊す。この用途では誤爆の損の方が大きい。
 * - 脚注、定義リスト、HTML タグの直書き。
 */

import { useState } from "react";
import type { ReactNode } from "react";

import { Icon } from "./icons";

/** 本文中の画像。
 *
 * **読み込みに失敗したらリンクに落とす。** 壊れた画像アイコンだけが残ると、
 * 何が入っていたのか分からなくなる。失敗する理由はいくつもある（KB が
 * private でブラウザに閲覧権限が無い、添付が消された、経路が塞がれている）
 * ので、原因を特定せずに扱えるこの形にしてある。
 *
 * なお画像はブラウザが KB から直接取りに行く。本文と違って**アプリの
 * トークンは載らない**ので、閲覧者自身に権限が要る。
 */
function MdImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <a href={src} target="_blank" rel="noreferrer" className="md-image-failed">
        <Icon name="image" />
        {alt || "画像を開く"}
      </a>
    );
  }
  return (
    <img
      className="md-image"
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

/** `javascript:` などを弾く。リンクは本文由来＝外部入力。 */
function safeHref(url: string): string | null {
  try {
    const parsed = new URL(url, location.origin);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

//         コード         太字          画像                 リンク               裸のURL
// 画像はリンクより先に置く。`![x](u)` は `[x](u)` を含むので、順番を逆にすると
// 画像が「! + リンク」として描かれてしまう
const INLINE =
  /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(!\[[^\]\n]*\]\([^)\s]+\))|(\[[^\]\n]*\]\([^)\s]+\))|(https?:\/\/[^\s<>()[\]]+)/g;

/** 行内の記法を描く。**コードの中は他の記法を解釈しない。** */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  let n = 0;
  INLINE.lastIndex = 0;

  while ((match = INLINE.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const [raw, code, bold, image, link, bare] = match;
    const key = `${keyPrefix}-${n++}`;

    if (code) {
      out.push(<code key={key}>{code.slice(1, -1)}</code>);
    } else if (bold) {
      out.push(<strong key={key}>{bold.slice(2, -2)}</strong>);
    } else if (image) {
      const split = image.indexOf("](");
      const alt = image.slice(2, split);
      const src = safeHref(image.slice(split + 2, -1));
      out.push(src ? <MdImage key={key} src={src} alt={alt} /> : raw);
    } else if (link) {
      const split = link.indexOf("](");
      const label = link.slice(1, split);
      const href = safeHref(link.slice(split + 2, -1));
      out.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noreferrer">
            {label || href}
          </a>
        ) : (
          raw
        ),
      );
    } else if (bare) {
      const href = safeHref(bare);
      out.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noreferrer">
            {bare}
          </a>
        ) : (
          raw
        ),
      );
    }
    last = match.index + raw.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)[-*+]\s+(.*)$/;
const NUMBERED = /^(\s*)\d+[.)]\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const RULE = /^\s*([-*_])\s*(\1\s*){2,}$/;
const FENCE = /^\s*```(.*)$/;
const TABLE_ROW = /^\s*\|(.+)\|\s*$/;
//                     | --- | :--: | ---: |
const TABLE_SEP = /^\s*\|[\s:|-]+\|\s*$/;

/** 表の 1 行をセルに割る。行頭・行末の `|` は既に落ちている前提。 */
function cells(line: string): string[] {
  return line
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((c) => c.trim());
}

type ListEntry = { indent: number; ordered: boolean; text: string };
type ListItem = { text: string; child: ListNode | null };
type ListNode = { ordered: boolean; items: ListItem[] };

/** 字下がりの深さから入れ子を組む。
 *
 * 深さは**その行の実際の空白数**で測る。Markdown の仕様上は 2 でも 4 でも
 * 良く、書き手によって混在するので、固定幅で割らずに前後の大小だけを見る。
 */
function buildList(entries: ListEntry[]): ListNode {
  const root: ListNode = { ordered: entries[0].ordered, items: [] };
  const stack: { indent: number; node: ListNode }[] = [
    { indent: entries[0].indent, node: root },
  ];

  for (const entry of entries) {
    while (stack.length > 1 && entry.indent < stack[stack.length - 1].indent) {
      stack.pop();
    }
    const top = stack[stack.length - 1];

    if (entry.indent > top.indent) {
      // 直前の項目の子になる。親が無い（いきなり深い）場合は同階層に倒す
      const parent = top.node.items[top.node.items.length - 1];
      if (!parent) {
        top.node.items.push({ text: entry.text, child: null });
        continue;
      }
      const child: ListNode = { ordered: entry.ordered, items: [] };
      parent.child = child;
      stack.push({ indent: entry.indent, node: child });
      child.items.push({ text: entry.text, child: null });
      continue;
    }
    top.node.items.push({ text: entry.text, child: null });
  }
  return root;
}

function hasNesting(node: ListNode): boolean {
  return node.items.some((i) => i.child !== null);
}

function renderList(node: ListNode, key: string, tree: boolean): ReactNode {
  const List = node.ordered ? "ol" : "ul";
  return (
    <List key={key} className={tree ? "md-tree" : undefined}>
      {node.items.map((item, i) => (
        <li key={i}>
          <span className="md-node">{inline(item.text, `${key}-${i}`)}</span>
          {item.child && renderList(item.child, `${key}-${i}-c`, tree)}
        </li>
      ))}
    </List>
  );
}

/** Markdown をブロック単位で描く。 */
export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let i = 0;

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    // 段落内の改行は保つ。KB の本文は 1 行 1 事実で書かれていることが多い
    blocks.push(
      <p key={`p${blocks.length}`}>{inline(paragraph.join("\n"), `p${blocks.length}`)}</p>,
    );
    paragraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(FENCE);
    if (fence) {
      flushParagraph();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) body.push(lines[i++]);
      i += 1; // 閉じ側。無いまま終端に達した場合もここで抜ける
      blocks.push(
        <pre key={`c${blocks.length}`}>
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      flushParagraph();
      blocks.push(<hr key={`h${blocks.length}`} />);
      i += 1;
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      flushParagraph();
      // 本文中の見出しは、画面の見出し階層より下に置く（h4 が知識の題名）
      const level = Math.min(heading[1].length, 6);
      const Tag = `h${Math.min(level + 4, 6)}` as "h5" | "h6";
      blocks.push(
        <Tag key={`t${blocks.length}`} className={`md-h${level}`}>
          {inline(heading[2], `t${blocks.length}`)}
        </Tag>,
      );
      i += 1;
      continue;
    }

    const quote = line.match(QUOTE);
    if (quote) {
      flushParagraph();
      const body: string[] = [];
      while (i < lines.length) {
        const m = lines[i].match(QUOTE);
        if (!m) break;
        body.push(m[1]);
        i += 1;
      }
      blocks.push(
        <blockquote key={`q${blocks.length}`}>
          {inline(body.join("\n"), `q${blocks.length}`)}
        </blockquote>,
      );
      continue;
    }

    // 表: ヘッダ行の次が区切り行のときだけ表とみなす（`|` を含むだけの
    // 普通の文を表に化けさせない）
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      flushParagraph();
      const header = cells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(cells(lines[i]));
        i += 1;
      }
      const key = `tb${blocks.length}`;
      blocks.push(
        <ul key={key} className="md-tree md-table">
          {rows.map((row, r) => (
            <li key={r}>
              <span className="md-node">{inline(row[0] ?? "", `${key}-${r}`)}</span>
              {row.length > 1 && (
                <ul>
                  {row.slice(1).map((cell, c) => (
                    <li key={c}>
                      <span className="md-node">
                        <span className="md-cell-label">
                          {header[c + 1] ?? `列${c + 2}`}
                        </span>
                        {inline(cell, `${key}-${r}-${c}`)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>,
      );
      continue;
    }

    const ordered = NUMBERED.test(line);
    if (ordered || BULLET.test(line)) {
      flushParagraph();
      const entries: ListEntry[] = [];
      while (i < lines.length) {
        const bullet = lines[i].match(BULLET);
        const numbered = lines[i].match(NUMBERED);
        const m = numbered ?? bullet;
        if (!m) break;
        entries.push({
          indent: m[1].length,
          ordered: numbered !== null,
          text: m[2],
        });
        i += 1;
      }
      const node = buildList(entries);
      // **入れ子があるときだけツリーにする。** 平らな箇条書きは普通の
      // 中黒・番号の方が読みやすく、罫線は情報を足さない
      blocks.push(renderList(node, `l${blocks.length}`, hasNesting(node)));
      continue;
    }

    paragraph.push(line);
    i += 1;
  }
  flushParagraph();

  return <div className="md">{blocks}</div>;
}
