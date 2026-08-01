import { useEffect, useState } from "react";
import { Account } from "./screens/Account";
import { Chat } from "./screens/Chat";
import { Developer } from "./screens/Developer";
import { Settings } from "./screens/Settings";
import { api } from "./api";
import { Icon, type IconName } from "./icons";
import { clearIdentity, loadIdentity, type Identity } from "./session";

type Screen = "chat" | "account" | "settings" | "developer";

const SCREENS: {
  id: Screen;
  label: string;
  icon: IconName;
  needsIdentity: boolean;
}[] = [
  { id: "chat", label: "チャット", icon: "chat", needsIdentity: true },
  { id: "account", label: "アカウント管理", icon: "user", needsIdentity: false },
  { id: "settings", label: "管理設定", icon: "sliders", needsIdentity: true },
  { id: "developer", label: "開発者向け", icon: "terminal", needsIdentity: false },
];

const SCREEN_IDS = SCREENS.map((s) => s.id);

function screenFromHash(fallback: Screen): Screen {
  const hash = location.hash.replace("#", "") as Screen;
  return SCREEN_IDS.includes(hash) ? hash : fallback;
}

export function App() {
  const [identity, setIdentity] = useState<Identity | null>(loadIdentity);
  const [screen, setScreen] = useState<Screen>(
    screenFromHash(identity ? "chat" : "account"),
  );
  const [unread, setUnread] = useState(0);

  // URL で画面を指せるようにする。ブックマークと戻るボタンが効く
  useEffect(() => {
    if (location.hash.replace("#", "") !== screen) location.hash = screen;
  }, [screen]);

  useEffect(() => {
    const onHash = () => setScreen(screenFromHash(screen));
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, [screen]);

  // 未読件数はチャット画面以外にいるときも出す。見落とさないようにするため
  useEffect(() => {
    if (!identity) return;
    let alive = true;
    const poll = () =>
      api
        .notifications(identity.tenantId)
        .then((n) => alive && setUnread(n.length))
        .catch(() => {});
    poll();
    const timer = setInterval(poll, 30_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [identity, screen]);

  return (
    <div className="app">
      <aside className="sidebar">
        <h1>知識ベース検索</h1>
        <nav>
          {SCREENS.map((s) => (
            <button
              key={s.id}
              className={screen === s.id ? "active" : ""}
              onClick={() => setScreen(s.id)}
              disabled={s.needsIdentity && !identity}
              title={s.needsIdentity && !identity ? "先にアカウントを選んでください" : ""}
            >
              <span className="nav-label">
                <Icon name={s.icon} />
                {s.label}
              </span>
              {s.id === "chat" && unread > 0 && <span className="pill">{unread}</span>}
            </button>
          ))}
        </nav>

        <div className="who">
          {identity ? (
            <>
              <strong>{identity.accountName}</strong>
              {identity.tenantName}
              <button
                style={{ marginTop: 8, width: "100%" }}
                onClick={() => {
                  clearIdentity();
                  setIdentity(null);
                  setScreen("account");
                }}
              >
                <Icon name="switch" />
                切り替え
              </button>
            </>
          ) : (
            <span>アカウント未選択</span>
          )}
        </div>
      </aside>

      <main>
        {/* 身元が要る画面に直接来たとき、白紙にせず理由を出す。
            URL で画面を指せるので、この状態には普通に到達する */}
        {SCREENS.find((s) => s.id === screen)?.needsIdentity && !identity && (
          <>
            <h2>{SCREENS.find((s) => s.id === screen)?.label}</h2>
            <div className="notice">
              この画面を使うには、先にアカウントを選んでください。
              <div>
                <button
                  className="primary"
                  style={{ marginTop: 8 }}
                  onClick={() => setScreen("account")}
                >
                  <Icon name="user" />
                  アカウント管理へ
                </button>
              </div>
            </div>
          </>
        )}

        {screen === "chat" && identity && <Chat identity={identity} />}
        {screen === "account" && (
          <Account
            identity={identity}
            onChange={(next) => {
              setIdentity(next);
              setScreen("chat");
            }}
          />
        )}
        {screen === "settings" && identity && <Settings identity={identity} />}
        {screen === "developer" && <Developer identity={identity} />}
      </main>
    </div>
  );
}
