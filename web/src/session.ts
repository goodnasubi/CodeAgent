// localStorage が持つもの。
//
// アカウントの識別情報と、会話履歴の**キャッシュ**だけ。会話履歴の正は
// サーバー側の DB で、端末を跨いでも同じ会話を続けられるようにしてある。
// 認証情報（KB のトークン等）はここには決して置かない。

import type { Message } from "./api";

const IDENTITY_KEY = "kb.identity";
const CACHE_PREFIX = "kb.messages.";

export interface Identity {
  tenantId: string;
  accountId: string;
  accountName: string;
  tenantName: string;
}

export function loadIdentity(): Identity | null {
  try {
    const raw = localStorage.getItem(IDENTITY_KEY);
    return raw ? (JSON.parse(raw) as Identity) : null;
  } catch {
    return null;
  }
}

export function saveIdentity(identity: Identity): void {
  localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
}

export function clearIdentity(): void {
  localStorage.removeItem(IDENTITY_KEY);
}

/** 会話の下書き表示用キャッシュ。サーバーから取り直すまでのつなぎ。 */
export function cacheMessages(conversationId: string, messages: Message[]): void {
  try {
    localStorage.setItem(CACHE_PREFIX + conversationId, JSON.stringify(messages));
  } catch {
    // 容量超過などは無視してよい。正は DB 側にある
  }
}

export function cachedMessages(conversationId: string): Message[] {
  try {
    const raw = localStorage.getItem(CACHE_PREFIX + conversationId);
    return raw ? (JSON.parse(raw) as Message[]) : [];
  } catch {
    return [];
  }
}
