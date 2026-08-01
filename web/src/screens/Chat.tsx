import { useCallback, useEffect, useRef, useState } from "react";
import type { ClipboardEvent } from "react";
import { Icon } from "../icons";
import { api, ApiError } from "../api";
import type {
  Conversation,
  Extracted,
  Message,
  Notification,
  SearchHit,
} from "../api";
import { cacheMessages, cachedMessages, type Identity } from "../session";

const SOURCE_LABEL: Record<string, string> = {
  vector: "意味",
  keyword: "言葉",
  graph: "つながり",
};

/** 検索が「なぜこれを出したか」を利用者に見せる。 */
function Hit({ hit }: { hit: SearchHit }) {
  return (
    <div className="hit">
      <div className="title">{hit.title || `#${hit.kb_issue_id}`}</div>
      <div className="meta">
        {hit.sources.map((s) => (
          <span key={s} className={`badge ${s}`}>
            {SOURCE_LABEL[s] ?? s}
          </span>
        ))}
        {hit.labels.map((l) => (
          <span key={l} className="badge label">
            {l}
          </span>
        ))}
        {hit.url && (
          <a href={hit.url} target="_blank" rel="noreferrer">
            開く
          </a>
        )}
      </div>
    </div>
  );
}

export function Chat({ identity }: { identity: Identity }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [skipped, setSkipped] = useState<Record<string, string>>({});
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);
  const [form, setForm] = useState<{ title: string; body: string } | null>(null);
  const [searched, setSearched] = useState(false);
  /** 送信したがサーバーから返ってきていない発言。**先に画面へ出すために持つ。**
   *  検索は 1 秒前後かかり、その間これが無いと入力欄が消えるだけで
   *  何も起きていないように見える。 */
  const [pending, setPending] = useState<string | null>(null);
  const [attaching, setAttaching] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const loadConversations = useCallback(async () => {
    const list = await api.conversations(identity.tenantId, identity.accountId);
    setConversations(list);
    return list;
  }, [identity]);

  useEffect(() => {
    loadConversations()
      .then((list) => list[0] && setCurrent(list[0].id))
      .catch((e) => setError(String(e)));
    api.notifications(identity.tenantId).then(setNotifications).catch(() => {});
  }, [identity, loadConversations]);

  useEffect(() => {
    if (!current) {
      setMessages([]);
      return;
    }
    // キャッシュを先に出してから、正であるサーバーの内容で置き換える
    setMessages(cachedMessages(current));
    api
      .messages(current)
      .then((m) => {
        setMessages(m);
        cacheMessages(current, m);
      })
      .catch((e) => setError(String(e)));
  }, [current]);

  useEffect(() => {
    // 発言が無いうちは動かさない。空の状態で呼ぶとページごとスクロールする
    if (messages.length === 0 && pending === null) return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, pending]);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    // 往復を待たずに自分の発言を出す。サーバーの内容で後から置き換わる
    setPending(text);
    setDraft("");
    try {
      let conversationId = current;
      if (!conversationId) {
        const created = await api.createConversation(
          identity.tenantId,
          identity.accountId,
        );
        conversationId = created.id;
        setCurrent(created.id);
      }

      await api.addMessage(conversationId, "user", text);

      const found = await api.search(identity.tenantId, text);
      setHits(found.results);
      setSkipped(found.skipped);
      setSearched(true);

      const reply = found.results.length
        ? `似た記録を ${found.results.length} 件見つけました。右の一覧から確認できます。`
        : "見つかりませんでした。新しい知識として登録できます。";
      await api.addMessage(conversationId, "assistant", reply);

      const refreshed = await api.messages(conversationId);
      setMessages(refreshed);
      cacheMessages(conversationId, refreshed);
      await loadConversations();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      // 書いた文章を消したままにしない。入力中でなければ入力欄に戻す
      setDraft((d) => d || text);
    } finally {
      setPending(null);
      setBusy(false);
    }
  }

  /** ファイル・URL の中身を本文テキストにして、入力欄に足す。
   *
   * 隠し持たずに入力欄へ出すのは、何が検索・登録に使われるのかを
   * 利用者が見て直せるようにするため。
   */
  async function absorb(run: () => Promise<Extracted>) {
    setAttaching(true);
    setError(null);
    setNote(null);
    try {
      const doc = await run();
      if (!doc.text.trim()) {
        setNote(
          `${doc.source_name} から文字を取り出せませんでした。` +
            "（画像の文字起こしは、LLM を設定すると使えるようになります）",
        );
        return;
      }
      setDraft((prev) =>
        `${prev.trim()}\n\n--- ${doc.source_name} ---\n${doc.text}`.trim(),
      );
      setNote(
        doc.truncated
          ? `${doc.source_name} は長いため、途中まで取り込みました。`
          : `${doc.source_name} を取り込みました。`,
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setAttaching(false);
    }
  }

  /** 貼り付けた画像やファイルも同じ経路で取り込む。 */
  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const file = Array.from(e.clipboardData.files)[0];
    if (!file) return; // 文字の貼り付けは既定の動作に任せる
    e.preventDefault();
    absorb(() => api.extractFile(identity.tenantId, file));
  }

  /** 直近の質問を下書きにして登録フォームを開く。 */
  function openForm() {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    const text = lastUser?.content ?? draft.trim();
    setForm({ title: text.slice(0, 60), body: text });
  }

  async function submitForm() {
    if (!form || !form.title.trim()) return;
    setRegistering(true);
    setError(null);
    try {
      const created = await api.register(
        identity.tenantId,
        form.title.trim(),
        form.body,
      );
      setForm(null);
      if (current) {
        await api.addMessage(
          current,
          "assistant",
          `新しい知識として登録しました: ${created.title}`,
        );
        const refreshed = await api.messages(current);
        setMessages(refreshed);
        cacheMessages(current, refreshed);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRegistering(false);
    }
  }

  async function dismiss(id: number) {
    await api.markRead(identity.tenantId, [id]);
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  }

  return (
    <>
      <h2>チャット</h2>
      <p className="lede">
        困りごとを普段の言葉で書いてください。似た記録を探し、見つからなければ新しく登録できます。
      </p>

      {error && <div className="notice error">{error}</div>}

      <div className="chat">
        <div className="side">
          <h3>
            <Icon name="list" />
            会話
          </h3>
          <button
            className={`new-conv ${current === null ? "active" : ""}`}
            onClick={() => {
              // **サーバーには作らない。** 会話は最初の発言のときに send() が
              // 作る。ここで先に作ると、題名は最初の発言から付けるため、
              // 一度も発言しなかった会話が「（無題）」のまま履歴に残る。
              setCurrent(null);
              setHits([]);
              setSkipped({});
              setSearched(false);
              setForm(null);
              setNote(null);
              setError(null);
            }}
            style={{ width: "100%", marginBottom: 8 }}
          >
            <Icon name="plus" />
            新しい会話
          </button>
          {conversations.map((c) => (
            <button
              key={c.id}
              className={`conv ${c.id === current ? "active" : ""}`}
              onClick={() => setCurrent(c.id)}
            >
              {c.title || "（無題）"}
            </button>
          ))}
          {conversations.length === 0 && (
            <p className="muted">まだ会話がありません</p>
          )}
        </div>

        <div className="thread">
          <div className="messages">
            {messages.map((m) => (
              <div key={m.id} className={`bubble ${m.role}`}>
                {m.content}
              </div>
            ))}
            {pending !== null && (
              <>
                <div className="bubble user">{pending}</div>
                <div className="bubble searching" role="status" aria-live="polite">
                  <span className="dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  似た記録を探しています…
                </div>
              </>
            )}
            {messages.length === 0 && pending === null && (
              <p className="muted">下の欄に質問を書いてください。</p>
            )}
            <div ref={endRef} />
          </div>
          <div className="sources">
            <button
              onClick={() => fileRef.current?.click()}
              disabled={attaching}
            >
              <Icon name="paperclip" />
              {attaching ? "読み取り中…" : "ファイルを選ぶ"}
            </button>
            <input
              ref={fileRef}
              type="file"
              style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                // 同じファイルをもう一度選べるように値を戻す
                e.target.value = "";
                if (file) absorb(() => api.extractFile(identity.tenantId, file));
              }}
            />
            <input
              value={urlDraft}
              placeholder="https://… の中身を取り込む"
              onChange={(e) => setUrlDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key !== "Enter" || !urlDraft.trim()) return;
                absorb(() => api.extractUrl(identity.tenantId, urlDraft.trim()));
                setUrlDraft("");
              }}
            />
            <button
              onClick={() => {
                absorb(() => api.extractUrl(identity.tenantId, urlDraft.trim()));
                setUrlDraft("");
              }}
              disabled={attaching || !urlDraft.trim()}
            >
              <Icon name="download" />
              取り込む
            </button>
          </div>
          {note && <p className="muted" style={{ margin: "4px 2px 0" }}>{note}</p>}
          <div className="composer">
            <textarea
              value={draft}
              placeholder="例: ORA-01555 というエラーが出ました（画像やファイルの貼り付けもできます）"
              onChange={(e) => setDraft(e.target.value)}
              onPaste={onPaste}
              onKeyDown={(e) => {
                if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
                // 抑えないと、送信と同時に改行も入る。send() が空文字や
                // 送信中で弾いたときは、その改行だけが残ってしまう
                e.preventDefault();
                send();
              }}
            />
            <button className="primary" onClick={send} disabled={busy || !draft.trim()}>
              <Icon name="send" />
              {busy ? "検索中…" : "送信"}
            </button>
          </div>
          <p className="muted composer-hint">
            <kbd>Ctrl</kbd> + <kbd>Enter</kbd> で送信します（Mac は <kbd>⌘</kbd>{" "}
            + <kbd>Enter</kbd>）。<kbd>Enter</kbd> だけなら改行です。
          </p>
        </div>

        <div className="side">
          {notifications.length > 0 && (
            <>
              <h3>
                <Icon name="bell" />
                お知らせ
              </h3>
              {notifications.map((n) => (
                <div key={n.id} className="notice">
                  <strong>{n.label}</strong> {n.title}
                  <div>
                    <button onClick={() => dismiss(n.id)} style={{ marginTop: 6 }}>
                      <Icon name="check" />
                      既読にする
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          <h3>
            <Icon name="search" />
            見つかった知識
          </h3>
          {/* 検索中は前回の結果を出したままにしない。もう古い */}
          {busy ? (
            <div aria-hidden="true">
              {[0, 1, 2].map((i) => (
                <div key={i} className="hit-skeleton">
                  <span className="line" style={{ width: "72%" }} />
                  <span className="line" style={{ width: "44%" }} />
                </div>
              ))}
            </div>
          ) : (
            <>
              {hits.map((h) => (
                <Hit key={h.kb_issue_id} hit={h} />
              ))}
              {hits.length === 0 && (
                <p className="muted">
                  {searched
                    ? "見つかりませんでした。新しい知識として登録できます。"
                    : "まだ検索していません"}
                </p>
              )}
            </>
          )}

          {/* 見つかった場合でも登録したいことがあるので、常に出す */}
          {form === null ? (
            <button
              className={hits.length === 0 && searched ? "primary" : ""}
              onClick={openForm}
              disabled={messages.length === 0 && !draft.trim()}
              style={{ width: "100%", marginTop: 8 }}
            >
              <Icon name="filePlus" />
              新しい知識として登録
            </button>
          ) : (
            <div className="card" style={{ marginTop: 10, padding: 12 }}>
              <div className="field">
                <label htmlFor="nt">題名</label>
                <input
                  id="nt"
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="nb">内容</label>
                <textarea
                  id="nb"
                  rows={5}
                  value={form.body}
                  onChange={(e) => setForm({ ...form, body: e.target.value })}
                />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  className="primary"
                  onClick={submitForm}
                  disabled={registering || !form.title.trim()}
                >
                  <Icon name="check" />
                  {registering ? "登録中…" : "登録する"}
                </button>
                <button onClick={() => setForm(null)} disabled={registering}>
                  <Icon name="x" />
                  やめる
                </button>
              </div>
              <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>
                知識ベース側に作成されます。
              </p>
            </div>
          )}

          {Object.keys(skipped).length > 0 && (
            <p className="muted" style={{ marginTop: 12 }}>
              使わなかった検索:{" "}
              {Object.entries(skipped)
                .map(([k, v]) => `${SOURCE_LABEL[k] ?? k}（${v}）`)
                .join(" / ")}
            </p>
          )}
        </div>
      </div>
    </>
  );
}
