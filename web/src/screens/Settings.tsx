import { useEffect, useState } from "react";
import { Icon } from "../icons";
import { api, ApiError } from "../api";
import type { KbConnection, KbType, ModelSettings, NotificationRule } from "../api";
import type { Identity } from "../session";

const KB_LABEL: Record<KbType, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  redmine: "Redmine",
  relation: "Re:lation",
};

/** 知識ベースごとに要る項目が違うので、入力欄の説明を変える。 */
const PROJECT_HINT: Record<KbType, string> = {
  github: "owner/repo の形式",
  gitlab: "group/project の形式",
  redmine: "プロジェクト識別子",
  relation: "サブドメイン",
};

const CHANNELS = [
  { value: "in_app", label: "チャット画面" },
  { value: "slack", label: "Slack" },
  { value: "email", label: "メール" },
];

export function Settings({ identity }: { identity: Identity }) {
  const [kb, setKb] = useState<KbConnection | null>(null);
  const [kbType, setKbType] = useState<KbType>("github");
  const [project, setProject] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [messageBoxId, setMessageBoxId] = useState("");
  const [token, setToken] = useState("");
  const [models, setModels] = useState<ModelSettings | null>(null);
  // APIキーは models と分けて持つ。取得しても値は返ってこないため、
  // 「入力があったときだけ送る」を素直に書けるようにする
  const [llmKey, setLlmKey] = useState("");
  const [embeddingKey, setEmbeddingKey] = useState("");
  const [rules, setRules] = useState<NotificationRule[]>([]);
  const [newRule, setNewRule] = useState<NotificationRule>({
    label: "",
    channel: "in_app",
    destination: "",
  });
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function reload() {
    const [connection, modelSettings, ruleList] = await Promise.all([
      api.getKb(identity.tenantId),
      api.getModels(identity.tenantId),
      api.listRules(identity.tenantId),
    ]);
    setKb(connection);
    setModels(modelSettings);
    setRules(ruleList);
    if (connection) {
      setKbType(connection.kb_type);
      setProject(connection.project);
      setBaseUrl(connection.base_url ?? "");
      setMessageBoxId(String(connection.extra?.message_box_id ?? ""));
    }
  }

  useEffect(() => {
    reload().catch((e) => setMessage({ ok: false, text: String(e) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity.tenantId]);

  async function saveKb() {
    setMessage(null);
    try {
      await api.setKb(identity.tenantId, {
        kb_type: kbType,
        project,
        token,
        base_url: baseUrl || null,
        extra: kbType === "relation" ? { message_box_id: Number(messageBoxId) } : {},
      });
      setToken("");
      await reload();
      setMessage({ ok: true, text: "知識ベースの設定を保存しました" });
    } catch (e) {
      setMessage({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    }
  }

  return (
    <>
      <h2>管理設定</h2>
      <p className="lede">
        このテナントが使う知識ベースと、対話・検索に使うモデルを設定します。
      </p>

      {message && (
        <div className={`notice ${message.ok ? "ok" : "error"}`}>{message.text}</div>
      )}

      <div className="card">
        <h3>
          <Icon name="database" />
          知識ベース
        </h3>
        <p className="muted" style={{ marginTop: 0 }}>
          テナントごとに 1 つだけ選びます。複数を同時には使いません。
        </p>

        <div className="row">
          <div className="field">
            <label htmlFor="kbType">種類</label>
            <select
              id="kbType"
              value={kbType}
              onChange={(e) => setKbType(e.target.value as KbType)}
            >
              {(Object.keys(KB_LABEL) as KbType[]).map((t) => (
                <option key={t} value={t}>
                  {KB_LABEL[t]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="project">接続先（{PROJECT_HINT[kbType]}）</label>
            <input
              id="project"
              value={project}
              onChange={(e) => setProject(e.target.value)}
              placeholder={PROJECT_HINT[kbType]}
            />
          </div>
        </div>

        <div className="row">
          <div className="field">
            <label htmlFor="baseUrl">
              API のURL{kbType === "redmine" ? "（必須）" : "（self-hosted の場合）"}
            </label>
            <input
              id="baseUrl"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://redmine.example.co.jp"
            />
          </div>
          {kbType === "relation" && (
            <div className="field">
              <label htmlFor="mbox">受信箱ID</label>
              <input
                id="mbox"
                value={messageBoxId}
                onChange={(e) => setMessageBoxId(e.target.value)}
              />
            </div>
          )}
        </div>

        <div className="field">
          <label htmlFor="token">アクセストークン</label>
          <input
            id="token"
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={kb ? "変更する場合のみ入力" : ""}
          />
        </div>

        <button className="primary" onClick={saveKb} disabled={!project || !token}>
          <Icon name="save" />
          保存
        </button>

        {kb && (
          <p className="muted" style={{ marginTop: 12 }}>
            現在の設定: <code>{KB_LABEL[kb.kb_type]}</code> / <code>{kb.project}</code>
            {kb.supports_keyword_search === false && (
              <>
                <br />
                この知識ベースは<strong>キーワード検索に対応していません</strong>
                。意味による検索だけで動きます。
              </>
            )}
            {kb.supports_relations === false && (
              <>
                <br />
                知識どうしのつながりも扱えないため、その検索は使われません。
              </>
            )}
          </p>
        )}
      </div>

      <div className="card">
        <h3>
          <Icon name="cpu" />
          モデル
        </h3>
        {models && (
          <>
            <div className="row">
              <div className="field">
                <label htmlFor="llm">画像の文字起こしに使うLLM</label>
                <select
                  id="llm"
                  value={models.llm_provider}
                  onChange={(e) =>
                    setModels({ ...models, llm_provider: e.target.value })
                  }
                >
                  <option value="gemini">Gemini</option>
                  <option value="claude">Claude API（未対応）</option>
                  <option value="openai">GPT-5.x（未対応）</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="llm-model">LLMのモデル名</label>
                <input
                  id="llm-model"
                  value={models.llm_model}
                  placeholder="空欄なら既定（gemini-3.6-flash）"
                  onChange={(e) =>
                    setModels({ ...models, llm_model: e.target.value })
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="llm-key">LLMのAPIキー</label>
                <input
                  id="llm-key"
                  type="password"
                  value={llmKey}
                  placeholder={models.has_llm_api_key ? "設定済み（変更する場合のみ入力）" : "未設定"}
                  onChange={(e) => setLlmKey(e.target.value)}
                />
              </div>
            </div>
            <div className="row">
              <div className="field">
                <label htmlFor="emb-provider">検索に使うembedding</label>
                <select
                  id="emb-provider"
                  value={models.embedding_provider}
                  onChange={(e) =>
                    setModels({ ...models, embedding_provider: e.target.value })
                  }
                >
                  <option value="gemini">Gemini</option>
                  <option value="hashing">開発用（意味は捉えません）</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="emb">embeddingのモデル名</label>
                <input
                  id="emb"
                  value={models.embedding_model}
                  placeholder="空欄なら既定（gemini-embedding-001）"
                  onChange={(e) =>
                    setModels({ ...models, embedding_model: e.target.value })
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="emb-dim">次元数</label>
                <input
                  id="emb-dim"
                  type="number"
                  value={models.embedding_dim}
                  onChange={(e) =>
                    setModels({ ...models, embedding_dim: Number(e.target.value) })
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="emb-key">embeddingのAPIキー</label>
                <input
                  id="emb-key"
                  type="password"
                  value={embeddingKey}
                  placeholder={
                    models.has_embedding_api_key ? "設定済み（変更する場合のみ入力）" : "未設定"
                  }
                  onChange={(e) => setEmbeddingKey(e.target.value)}
                />
              </div>
              <button
                onClick={async () => {
                  await api.setModels(identity.tenantId, {
                    llm_provider: models.llm_provider,
                    llm_model: models.llm_model,
                    embedding_provider: models.embedding_provider,
                    embedding_model: models.embedding_model,
                    embedding_dim: models.embedding_dim,
                    // 空欄のときは送らない。送ると保存済みのキーを消してしまう
                    ...(llmKey ? { llm_api_key: llmKey } : {}),
                    ...(embeddingKey ? { embedding_api_key: embeddingKey } : {}),
                  });
                  setLlmKey("");
                  setEmbeddingKey("");
                  setModels(await api.getModels(identity.tenantId));
                  setMessage({ ok: true, text: "モデル設定を保存しました" });
                }}
              >
                <Icon name="save" />
                保存
              </button>
            </div>
            <p className="muted">
              APIキーは暗号化して保存され、画面には二度と返しません。次元数は2,000以下にしてください（それを超えると検索インデックスを作れません）。embeddingモデルや次元数を変えると、そのテナントの知識をすべて作り直す必要があります（異なるモデルのベクトルは比較できないため）。作り直しは開発者向け画面から手動で実行します。
            </p>
          </>
        )}
      </div>

      <div className="card">
        <h3>
          <Icon name="bell" />
          通知
        </h3>
        <p className="muted" style={{ marginTop: 0 }}>
          知識にラベルが付いたときの知らせ先を決めます。
        </p>

        <div className="row">
          <div className="field">
            <label htmlFor="rl">ラベル</label>
            <input
              id="rl"
              value={newRule.label}
              onChange={(e) => setNewRule({ ...newRule, label: e.target.value })}
              placeholder="重大"
            />
          </div>
          <div className="field">
            <label htmlFor="rc">知らせ先</label>
            <select
              id="rc"
              value={newRule.channel}
              onChange={(e) => setNewRule({ ...newRule, channel: e.target.value })}
            >
              {CHANNELS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="rd">宛先</label>
            <input
              id="rd"
              value={newRule.destination}
              onChange={(e) => setNewRule({ ...newRule, destination: e.target.value })}
              placeholder={
                newRule.channel === "slack"
                  ? "Webhook URL"
                  : newRule.channel === "email"
                    ? "メールアドレス"
                    : "（不要）"
              }
            />
          </div>
          <button
            onClick={async () => {
              await api.addRule(identity.tenantId, newRule);
              setNewRule({ label: "", channel: "in_app", destination: "" });
              setRules(await api.listRules(identity.tenantId));
            }}
            disabled={!newRule.label}
          >
            <Icon name="plus" />
            追加
          </button>
        </div>

        {rules.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>ラベル</th>
                <th>知らせ先</th>
                <th>宛先</th>
                <th style={{ width: 70 }} />
              </tr>
            </thead>
            <tbody>
              {rules.map((r, i) => (
                <tr key={i}>
                  <td>{r.label}</td>
                  <td>{CHANNELS.find((c) => c.value === r.channel)?.label ?? r.channel}</td>
                  <td>{r.destination || <span className="muted">—</span>}</td>
                  <td>
                    <button
                      className="danger"
                      onClick={async () => {
                        await api.removeRule(identity.tenantId, r);
                        setRules(await api.listRules(identity.tenantId));
                      }}
                    >
                      <Icon name="trash" />
                      削除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">まだ設定がありません。</p>
        )}
      </div>
    </>
  );
}
