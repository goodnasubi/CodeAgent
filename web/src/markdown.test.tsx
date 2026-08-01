/** Markdown 実装のテスト。
 *
 * 描画結果は renderToStaticMarkup で HTML 文字列にして確かめる。DOM を操作
 * するわけではないので、testing-library は入れていない。
 *
 * **落ちてほしいのは「記法が壊れて読めなくなる」変更**なので、見た目の
 * 細部（class 名や余白）ではなく、構造と、壊すと痛い境界を突く。
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown } from "./markdown";

const html = (text: string) => renderToStaticMarkup(<Markdown text={text} />);

describe("見出し", () => {
  it("階層に応じた見出しを出す", () => {
    expect(html("# 大見出し")).toContain("md-h1");
    expect(html("### 小見出し")).toContain("md-h3");
  });

  it("画面の見出し階層より下に置く（知識の題名が h4 のため）", () => {
    // h1〜h3 を本文が使うと、ページの見出し構造が壊れる
    const out = html("# 大見出し");
    expect(out).not.toMatch(/<h[1-4]/);
    expect(out).toMatch(/<h[56]/);
  });

  it("`#` だけの行は見出しにしない", () => {
    expect(html("#タグのようなもの")).toContain("<p>");
  });
});

describe("インライン記法", () => {
  it("コード・太字・リンクを描く", () => {
    expect(html("`code`")).toContain("<code>code</code>");
    expect(html("**強調**")).toContain("<strong>強調</strong>");
    expect(html("[ラベル](https://example.com/a)")).toContain(
      '<a href="https://example.com/a"',
    );
  });

  it("裸の URL もリンクにする", () => {
    expect(html("参考: https://example.com/x")).toContain(
      '<a href="https://example.com/x"',
    );
  });

  it("コードの中では他の記法を解釈しない", () => {
    const out = html("`**これは太字にしない**`");
    expect(out).not.toContain("<strong>");
    expect(out).toContain("**これは太字にしない**");
  });

  it("**斜体は解釈しない** — 技術的な文字列を壊すため", () => {
    // ここが崩れると UNDO_RETENTION が「UNDO<em>RETENTION</em>」になる
    const out = html("`UNDO_RETENTION` と _単なる下線_ と SELECT * FROM t");
    expect(out).not.toContain("<em>");
    expect(out).toContain("_単なる下線_");
    expect(out).toContain("SELECT * FROM t");
  });

  it("http/https 以外のリンクは素通しにする", () => {
    // 危険なのは href になることであって、本文に文字列として残ること自体は
    // 無害（React がエスケープする）。リンク化されないことだけを見る
    const out = html("[押すな](javascript:alert(1))");
    expect(out).not.toContain("<a ");
    expect(out).not.toContain("href=");
  });
});

describe("画像", () => {
  it("`![]()` を img にする", () => {
    const out = html("![説明](https://example.com/a.png)");
    expect(out).toContain('<img class="md-image"');
    expect(out).toContain('src="https://example.com/a.png"');
    expect(out).toContain('alt="説明"');
  });

  it("**画像はリンクより先に判定する**", () => {
    // 順序を誤ると `![x](u)` が「! + リンク」として描かれる
    const out = html("![x](https://example.com/a.png)");
    expect(out).toContain("<img");
    expect(out).not.toContain("<a ");
  });

  it("危険な scheme の画像は素通しにする", () => {
    expect(html("![x](javascript:alert(1))")).not.toContain("<img");
  });
});

describe("リスト", () => {
  it("平らな箇条書きはツリーにしない", () => {
    // 1 階層に罫線を引いても情報が増えず、重くなるだけ
    const out = html("- ひとつ\n- ふたつ");
    expect(out).not.toContain("md-tree");
    expect(out).toContain("<ul>");
  });

  it("入れ子があるときはツリーにする", () => {
    const out = html("- 親\n  - 子");
    expect(out).toContain("md-tree");
    // 子は親の <li> の内側にある
    expect(out).toMatch(/<li>.*親.*<ul[^>]*>.*子.*<\/ul><\/li>/s);
  });

  it("字下がりの幅は 2 でも 4 でも同じ階層になる", () => {
    // Markdown ではどちらも有効で、書き手によって混在する
    const two = html("- 親\n  - 子");
    const four = html("- 親\n    - 子");
    expect(two).toEqual(four);
  });

  it("3 階層以上も組める", () => {
    const out = html("- 1\n  - 2\n    - 3");
    expect((out.match(/<ul/g) ?? []).length).toBe(3);
  });

  it("字下がりが戻ると階層も戻る", () => {
    const out = html("- 親A\n  - 子\n- 親B");
    // 親B は最上位に戻る（子の兄弟にならない）
    expect(out).toMatch(/<\/ul><\/li><li><span[^>]*>親B/);
  });

  it("番号付きと箇条書きを区別する", () => {
    expect(html("1. 手順")).toContain("<ol");
    expect(html("- 項目")).toContain("<ul");
  });

  it("いきなり深い行があっても落ちない", () => {
    expect(() => html("    - 親のない子")).not.toThrow();
  });
});

describe("表", () => {
  const table = [
    "| 環境 | 版 | 発生 |",
    "|---|---|---|",
    "| 本番 | 19c | あり |",
    "| 検証 | 19c | なし |",
  ].join("\n");

  it("1 列目をノード、残りを見出し付きの子にする", () => {
    const out = html(table);
    expect(out).toContain("md-table");
    expect(out).toContain("本番");
    expect(out).toContain("md-cell-label");
    expect(out).toContain("版");
    expect(out).toContain("19c");
  });

  it("行数ぶんのノードを作る", () => {
    const out = html(table);
    expect(out).toContain("本番");
    expect(out).toContain("検証");
  });

  it("**区切り行が無ければ表にしない**", () => {
    // 本文中でたまたま `|` を含む文が表に化けるのを防ぐ
    const out = html("A | B のどちらかを選ぶ");
    expect(out).not.toContain("md-table");
    expect(out).toContain("<p>");
  });

  it("見出しより列が多い行でも落ちない", () => {
    const out = html("| A |\n|---|\n| 1 | 2 |");
    expect(() => out).not.toThrow();
    expect(out).toContain("列2");
  });
});

describe("ブロック", () => {
  it("コードブロックの中身をそのまま出す", () => {
    const out = html("```sql\nSELECT * FROM t;\n```");
    expect(out).toContain("<pre><code>SELECT * FROM t;</code></pre>");
  });

  it("コードブロックの中では記法を解釈しない", () => {
    const out = html("```\n# 見出しではない\n- 箇条書きでもない\n```");
    expect(out).not.toMatch(/<h[56]/);
    expect(out).not.toContain("<ul>");
  });

  it("閉じられていないコードブロックでも終端まで読んで止まる", () => {
    // ここが無限ループになると画面ごと固まる
    const out = html("```\nつづき");
    expect(out).toContain("つづき");
  });

  it("引用と水平線を描く", () => {
    expect(html("> 引用文")).toContain("<blockquote>");
    expect(html("---")).toContain("<hr/>");
  });

  it("段落内の改行は保つ", () => {
    // KB の本文は 1 行 1 事実で書かれていることが多い
    const out = html("1行目\n2行目");
    expect(out).toContain("1行目\n2行目");
  });

  it("空行で段落を分ける", () => {
    const out = html("前\n\n後");
    expect((out.match(/<p>/g) ?? []).length).toBe(2);
  });

  it("空文字でも落ちない", () => {
    expect(() => html("")).not.toThrow();
  });
});

describe("エスケープ", () => {
  it("**HTML を組み立てない** — 本文は外部入力", () => {
    const out = html("<script>alert(1)</script>");
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("コードブロックの中の HTML もエスケープする", () => {
    const out = html("```\n<img onerror=alert(1)>\n```");
    expect(out).not.toContain("<img");
    expect(out).toContain("&lt;img");
  });
});
