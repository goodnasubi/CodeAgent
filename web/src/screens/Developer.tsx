import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { SyncReport, SyncStatus, Tenant } from "../api";
import type { Identity } from "../session";

/** 開発者向け画面。テナントの払い出しと、取り込みの手動実行・状態確認。 */
export function Developer({ identity }: { identity: Identity | null }) {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [newTenant, setNewTenant] = useState("");
  const [target, setTarget] = useState(identity?.tenantId ?? "");
  const [accountName, setAccountName] = useState("");
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [report, setReport] = useState<SyncReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function reloadTenants() {
    setTenants(await api.listTenants());
  }

  useEffect(() => {
    reloadTenants().catch((e) => setMessage({ ok: false, text: String(e) }));
  }, []);

  useEffect(() => {
    if (!target) {
      setStatus(null);
      return;
    }
    api.syncStatus(target).then(setStatus).catch(() => setStatus(null));
  }, [target, report]);

  async function runSync() {
    setBusy(true);
    setMessage(null);
    setReport(null);
    try {
      setReport(await api.runSync(target));
    } catch (e) {
      setMessage({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2>開発者向け</h2>
      <p className="lede">
        テナントの払い出しと、取り込みの手動実行・状態確認を行います。
      </p>

      {message && (
        <div className={`notice ${message.ok ? "ok" : "error"}`}>{message.text}</div>
      )}

      <div className="card">
        <h3>テナントの払い出し</h3>
        <div className="row">
          <div className="field">
            <label htmlFor="tn">テナント名</label>
            <input
              id="tn"
              value={newTenant}
              onChange={(e) => setNewTenant(e.target.value)}
              placeholder="○○事業部"
            />
          </div>
          <button
            className="primary"
            disabled={!newTenant.trim()}
            onClick={async () => {
              const created = await api.createTenant(newTenant.trim());
              setNewTenant("");
              setTarget(created.id);
              await reloadTenants();
              setMessage({ ok: true, text: `テナント「${created.name}」を作成しました` });
            }}
          >
            作成
          </button>
        </div>

        {tenants.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>名前</th>
                <th>ID</th>
              </tr>
            </thead>
            <tbody>
              {tenants.map((t) => (
                <tr key={t.id}>
                  <td>{t.name}</td>
                  <td>
                    <code>{t.id}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>操作するテナント</h3>
        <div className="field">
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">選択してください</option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {target && (
        <>
          <div className="card">
            <h3>アカウントの払い出し</h3>
            <div className="row">
              <div className="field">
                <label htmlFor="an">表示名</label>
                <input
                  id="an"
                  value={accountName}
                  onChange={(e) => setAccountName(e.target.value)}
                  placeholder="山田"
                />
              </div>
              <button
                disabled={!accountName.trim()}
                onClick={async () => {
                  await api.createAccount(target, accountName.trim());
                  setAccountName("");
                  setMessage({ ok: true, text: "アカウントを作成しました" });
                }}
              >
                作成
              </button>
            </div>
          </div>

          <div className="card">
            <h3>知識ベースからの取り込み</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              本来は 10 分ごとに自動で走ります。ここでは手動で実行できます。
            </p>

            <button className="primary" onClick={runSync} disabled={busy}>
              {busy ? "実行中…" : "いま取り込む"}
            </button>

            {status && (
              <table style={{ marginTop: 14 }}>
                <tbody>
                  <tr>
                    <th style={{ width: 160 }}>ここまで取り込み済み</th>
                    <td>{status.last_synced_at ?? <span className="muted">未実行</span>}</td>
                  </tr>
                  <tr>
                    <th>最後に実行した時刻</th>
                    <td>{status.last_run_at ?? <span className="muted">—</span>}</td>
                  </tr>
                  <tr>
                    <th>直近のエラー</th>
                    <td>
                      {status.last_error ? (
                        <span style={{ color: "var(--danger)" }}>{status.last_error}</span>
                      ) : (
                        <span className="muted">なし</span>
                      )}
                    </td>
                  </tr>
                </tbody>
              </table>
            )}

            {report && (
              <div className="notice ok" style={{ marginTop: 14 }}>
                取り込み {report.ingested.length} 件 / 通知 {report.notified.length} 件
                {report.failed.length > 0 && (
                  <>
                    <br />
                    失敗 {report.failed.length} 件:{" "}
                    {report.failed.map((f) => `#${f.id}（${f.error}）`).join(", ")}
                  </>
                )}
                {report.aborted && (
                  <>
                    <br />
                    中止: {report.aborted}
                  </>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
