import { useEffect, useState } from "react";
import { Icon } from "../icons";
import { api } from "../api";
import type { Account as AccountRow, Tenant } from "../api";
import { saveIdentity, type Identity } from "../session";

/**
 * アカウント管理画面。
 *
 * できるのはアカウントの識別と切り替えまで。所属テナントは読み取り専用で、
 * ここからは変更できない（テナントは管理者が払い出すため）。
 * KB のトークンなど認証情報もこの画面では扱わない。
 */
export function Account({
  identity,
  onChange,
}: {
  identity: Identity | null;
  onChange: (identity: Identity) => void;
}) {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState(identity?.tenantId ?? "");
  const [accounts, setAccounts] = useState<AccountRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listTenants().then(setTenants).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!tenantId) {
      setAccounts([]);
      return;
    }
    api.listAccounts(tenantId).then(setAccounts).catch((e) => setError(String(e)));
  }, [tenantId]);

  function choose(account: AccountRow) {
    const tenant = tenants.find((t) => t.id === account.tenant_id);
    const next: Identity = {
      tenantId: account.tenant_id,
      accountId: account.id,
      accountName: account.display_name || account.id.slice(0, 8),
      tenantName: tenant?.name ?? "",
    };
    saveIdentity(next);
    onChange(next);
  }

  return (
    <>
      <h2>アカウント管理</h2>
      <p className="lede">
        利用するアカウントを選びます。テナントは管理者が払い出すもので、ここからは変更できません。
      </p>

      {error && <div className="notice error">{error}</div>}

      <div className="card">
        <h3>
          <Icon name="user" />
          現在のアカウント
        </h3>
        {identity ? (
          <table>
            <tbody>
              <tr>
                <th style={{ width: 120 }}>アカウント</th>
                <td>{identity.accountName}</td>
              </tr>
              <tr>
                <th>所属テナント</th>
                <td>
                  {identity.tenantName || identity.tenantId}{" "}
                  <span className="muted">（読み取り専用）</span>
                </td>
              </tr>
            </tbody>
          </table>
        ) : (
          <p className="muted">まだ選択されていません。下から選んでください。</p>
        )}
      </div>

      <div className="card">
        <h3>
          <Icon name="switch" />
          アカウントを選ぶ
        </h3>
        <div className="field">
          <label htmlFor="tenant">テナント</label>
          <select
            id="tenant"
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
          >
            <option value="">選択してください</option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>

        {tenantId && accounts.length === 0 && (
          <p className="muted">
            このテナントにはアカウントがありません。開発者向け画面で払い出してください。
          </p>
        )}

        {accounts.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>表示名</th>
                <th style={{ width: 90 }} />
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id}>
                  <td>{a.display_name || <span className="muted">（名前なし）</span>}</td>
                  <td>
                    <button
                      onClick={() => choose(a)}
                      disabled={a.id === identity?.accountId}
                    >
                      <Icon name="check" />
                  {a.id === identity?.accountId ? "使用中" : "使う"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <p className="muted">
        この画面では認証情報を扱いません。知識ベースのトークンはサーバー側で暗号化して保管され、ブラウザには渡されません。
      </p>
    </>
  );
}
