import { useEffect, useState } from "react";
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
        <h3>知識ベース</h3>
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
        <h3>モデル</h3>
        {models && (
          <>
            <div className="row">
              <div className="field">
                <label htmlFor="llm">対話・要約に使うLLM</label>
                <select
                  id="llm"
                  value={models.llm_provider}
                  onChange={(e) =>
                    setModels({ ...models, llm_provider: e.target.value })
                  }
                >
                  <option value="claude">Claude API</option>
                  <option value="openai">GPT-5.x</option>
                  <option value="gemini">Gemini</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="emb">検索に使うembeddingモデル</label>
                <input
                  id="emb"
                  value={models.embedding_model}
                  onChange={(e) =>
                    setModels({ ...models, embedding_model: e.target.value })
                  }
                />
              </div>
              <button
                onClick={async () => {
                  await api.setModels(identity.tenantId, models);
                  setMessage({ ok: true, text: "モデル設定を保存しました" });
                }}
              >
                保存
              </button>
            </div>
            <p className="muted">
              embeddingモデルを変えると、そのテナントの知識をすべて作り直す必要があります（異なるモデルのベクトルは比較できないため）。作り直しは開発者向け画面から手動で実行します。
            </p>
          </>
        )}
      </div>

      <div className="card">
        <h3>通知</h3>
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
