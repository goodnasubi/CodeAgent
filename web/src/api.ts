// サーバー API の薄いラッパー。
//
// 認証情報（KB のトークン等）はサーバー側 DB にあり、ここには決して現れない。

export type KbType = "github" | "gitlab" | "redmine" | "relation";

export interface Tenant {
  id: string;
  name: string;
}

export interface Account {
  id: string;
  tenant_id: string;
  display_name: string;
}

export interface KbConnection {
  kb_type: KbType;
  project: string;
  base_url: string | null;
  extra: Record<string, unknown>;
  supports_keyword_search: boolean | null;
  supports_relations: boolean | null;
}

export interface ModelSettings {
  llm_provider: string;
  llm_model: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_dim: number;
  /** APIキーそのものは返らない。設定済みかどうかだけ分かる。 */
  has_llm_api_key: boolean;
  has_embedding_api_key: boolean;
}

/** 保存時の本体。キーは入力があったときだけ送る（未指定なら保存済みの値が残る）。 */
export interface ModelSettingsUpdate {
  llm_provider: string;
  llm_model: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_dim: number;
  llm_api_key?: string;
  embedding_api_key?: string;
}

export interface SearchHit {
  kb_issue_id: string;
  title: string;
  url: string | null;
  score: number;
  sources: string[];
  labels: string[];
}

export interface SearchResponse {
  results: SearchHit[];
  used: string[];
  skipped: Record<string, string>;
}

export interface Notification {
  id: number;
  kb_issue_id: string;
  label: string;
  title: string;
  url: string | null;
}

export interface NotificationRule {
  label: string;
  channel: string;
  destination: string;
}

export interface Conversation {
  id: string;
  title: string;
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
}

export interface SyncStatus {
  last_synced_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
  /** 常駐スケジューラのポーリング間隔（秒）。設定で変えられる。 */
  interval_seconds: number;
}

export interface SyncReport {
  ingested: string[];
  notified: string[];
  failed: { id: string; error: string }[];
  aborted: string | null;
}

export interface Extracted {
  source_name: string;
  text: string;
  truncated: boolean;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData のときは Content-Type を書かない。手で書くと multipart の
  // boundary が付かず、サーバー側でパースできなくなる
  const isForm = init?.body instanceof FormData;
  const response = await fetch(path, {
    ...init,
    headers: isForm
      ? (init?.headers ?? {})
      : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    // サーバーが理由を返していればそれを見せる。汎用文言だと原因が分からない
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* JSON でない応答はそのまま */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown) => ({ body: JSON.stringify(body) });

export const api = {
  health: () => request<{ ok: boolean }>("/api/health"),

  // 開発者向け
  listTenants: () => request<Tenant[]>("/api/admin/tenants"),
  createTenant: (name: string) =>
    request<Tenant>("/api/admin/tenants", { method: "POST", ...json({ name }) }),
  createAccount: (tenantId: string, displayName: string) =>
    request<Account>(`/api/admin/tenants/${tenantId}/accounts`, {
      method: "POST",
      ...json({ display_name: displayName }),
    }),
  runSync: (tenantId: string) =>
    request<SyncReport>(`/api/admin/tenants/${tenantId}/sync`, { method: "POST" }),
  syncStatus: (tenantId: string) =>
    request<SyncStatus>(`/api/admin/tenants/${tenantId}/sync`),

  // 設定
  listAccounts: (tenantId: string) =>
    request<Account[]>(`/api/tenants/${tenantId}/accounts`),
  getKb: (tenantId: string) =>
    request<KbConnection | null>(`/api/tenants/${tenantId}/kb`),
  setKb: (tenantId: string, body: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/api/tenants/${tenantId}/kb`, {
      method: "PUT",
      ...json(body),
    }),
  getModels: (tenantId: string) =>
    request<ModelSettings>(`/api/tenants/${tenantId}/models`),
  setModels: (tenantId: string, body: ModelSettingsUpdate) =>
    request<{ ok: boolean }>(`/api/tenants/${tenantId}/models`, {
      method: "PUT",
      ...json(body),
    }),

  // 素材の取り込み
  extractFile: (tenantId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Extracted>(`/api/tenants/${tenantId}/extract/file`, {
      method: "POST",
      body: form,
    });
  },
  extractUrl: (tenantId: string, url: string) =>
    request<Extracted>(`/api/tenants/${tenantId}/extract/url`, {
      method: "POST",
      ...json({ url }),
    }),

  // 検索・知識
  search: (tenantId: string, query: string, limit = 10) =>
    request<SearchResponse>(`/api/tenants/${tenantId}/search`, {
      method: "POST",
      ...json({ query, limit }),
    }),
  register: (tenantId: string, title: string, body: string, labels: string[] = []) =>
    request<{ id: string; title: string; url: string }>(
      `/api/tenants/${tenantId}/knowledge`,
      { method: "POST", ...json({ title, body, labels }) },
    ),
  append: (tenantId: string, knowledgeId: string, text: string) =>
    request<{ ok: boolean }>(
      `/api/tenants/${tenantId}/knowledge/${knowledgeId}/append`,
      { method: "POST", ...json({ text }) },
    ),

  // 通知
  notifications: (tenantId: string) =>
    request<Notification[]>(`/api/tenants/${tenantId}/notifications`),
  markRead: (tenantId: string, ids: number[]) =>
    request<{ updated: number }>(`/api/tenants/${tenantId}/notifications/read`, {
      method: "POST",
      ...json({ ids }),
    }),
  listRules: (tenantId: string) =>
    request<NotificationRule[]>(`/api/tenants/${tenantId}/notification-rules`),
  addRule: (tenantId: string, rule: NotificationRule) =>
    request<{ ok: boolean }>(`/api/tenants/${tenantId}/notification-rules`, {
      method: "POST",
      ...json(rule),
    }),
  removeRule: (tenantId: string, rule: NotificationRule) =>
    request<{ ok: boolean }>(`/api/tenants/${tenantId}/notification-rules`, {
      method: "DELETE",
      ...json(rule),
    }),

  // 会話
  conversations: (tenantId: string, accountId: string) =>
    request<Conversation[]>(
      `/api/tenants/${tenantId}/conversations?account_id=${accountId}`,
    ),
  createConversation: (tenantId: string, accountId: string) =>
    request<Conversation>(
      `/api/tenants/${tenantId}/conversations?account_id=${accountId}`,
      { method: "POST" },
    ),
  messages: (conversationId: string) =>
    request<Message[]>(`/api/conversations/${conversationId}/messages`),
  addMessage: (conversationId: string, role: string, content: string) =>
    request<Message>(`/api/conversations/${conversationId}/messages`, {
      method: "POST",
      ...json({ role, content }),
    }),
};
