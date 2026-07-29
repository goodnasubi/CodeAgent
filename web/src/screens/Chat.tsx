import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { Conversation, Message, Notification, SearchHit } from "../api";
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
    if (messages.length === 0) return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages]);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
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
      setDraft("");

      const found = await api.search(identity.tenantId, text);
      setHits(found.results);
      setSkipped(found.skipped);

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
    } finally {
      setBusy(false);
    }
  }

  async function registerNew() {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    setRegistering(true);
    setError(null);
    try {
      const created = await api.register(
        identity.tenantId,
        lastUser.content.slice(0, 80),
        lastUser.content,
      );
      if (current) {
        await api.addMessage(
          current,
          "assistant",
          `新しい知識として登録しました: ${created.title}`,
        );
        setMessages(await api.messages(current));
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
          <h3>会話</h3>
          <button
            onClick={async () => {
              const created = await api.createConversation(
                identity.tenantId,
                identity.accountId,
              );
              setCurrent(created.id);
              await loadConversations();
            }}
            style={{ width: "100%", marginBottom: 8 }}
          >
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
            {messages.length === 0 && (
              <p className="muted">下の欄に質問を書いてください。</p>
            )}
            <div ref={endRef} />
          </div>
          <div className="composer">
            <textarea
              value={draft}
              placeholder="例: ORA-01555 というエラーが出ました"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
              }}
            />
            <button className="primary" onClick={send} disabled={busy || !draft.trim()}>
              {busy ? "検索中…" : "送信"}
            </button>
          </div>
        </div>

        <div className="side">
          {notifications.length > 0 && (
            <>
              <h3>お知らせ</h3>
              {notifications.map((n) => (
                <div key={n.id} className="notice">
                  <strong>{n.label}</strong> {n.title}
                  <div>
                    <button onClick={() => dismiss(n.id)} style={{ marginTop: 6 }}>
                      既読にする
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          <h3>見つかった知識</h3>
          {hits.map((h) => (
            <Hit key={h.kb_issue_id} hit={h} />
          ))}
          {hits.length === 0 && <p className="muted">まだ検索していません</p>}

          {hits.length === 0 && messages.some((m) => m.role === "user") && (
            <button onClick={registerNew} disabled={registering}>
              {registering ? "登録中…" : "新しい知識として登録"}
            </button>
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
