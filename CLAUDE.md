# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

This repository currently contains **design/requirements documentation only** — no source code, build tooling, package manifests, or tests exist yet. There is nothing to build, lint, or run. Once implementation begins, replace this section with actual commands (build, lint, test, run a single test, etc.).

## What's here

- [docs/knowledge-base-system-requirements.md](docs/knowledge-base-system-requirements.md) — **the source of truth.** A multi-backend, multi-tenant knowledge base system. Most design questions have now been resolved through a decision-by-decision review; each decision records its rationale, so read the reasoning before revisiting one.
- [docs/system-overview.svg](docs/system-overview.svg) — plain-language whole-system diagram aimed at non-engineers (PNG twin at `system-overview.png` for slides/email). It teaches the bookshelf/index-card metaphor for the source-of-truth split below; reuse that vocabulary when explaining the system to non-technical readers. Edit the SVG, then re-render the PNG — never edit them independently.
- [docs/knowledge-base-system-requirements.html](docs/knowledge-base-system-requirements.html) — illustrated version of the requirements (standalone page; also published as an Artifact).
- [docs/issue-similarity-search-design_1.md](docs/issue-similarity-search-design_1.md) — the original **GitLab-only** design memo, superseded by the above. Retained for pipeline-level detail that still applies: the image-OCR-into-body approach, the `combined_text` format with separator markers, the pgvector schema, and `glab` CLI invocation patterns (which will be reused for the keyword-search half of hybrid search).

## The one architectural fact that governs everything

**The external KB (GitLab / GitHub / Redmine) is the source of truth; pgvector is a derived search index.**

New knowledge is written to the KB as an Issue/ticket. pgvector holds only embeddings and combined text derived from it. Consequences that follow, and that most other decisions depend on:

- pgvector data can be discarded and rebuilt at any time — it is not a backup target.
- KB API outage blocks new registration; search degrades gracefully on the pgvector side.
- Labels are KB-native, so change detection and notification triggers ride the same polling mechanism.

If you find yourself designing something that treats the local DB as authoritative for knowledge content, stop — it contradicts this and several dependent decisions.

## Resolved design decisions

**Stack**: Python 3.10+ backend (chosen so [markitdown](https://github.com/microsoft/markitdown) is a direct library call, and for LLM/embedding SDK maturity), TypeScript + React frontend, PostgreSQL + pgvector.

**Tenancy**: One knowledge backend per tenant — never multiple queried together. Tenant ID scopes KB connection config, LLM/embedding config, accounts, and conversation history. Tenants are admin-provisioned via the developer screen; no self-service creation. KB credentials live in a separate `tenant_kb_connections` table (1:1 with tenant, app-level encrypted token), deliberately not on the tenant row — trigger rotation/enable/connection-test state accrete there, and separating it keeps tokens out of the frequently-read tenant record.

**LLM/embedding are both per-tenant selectable.** "ClaudeCode" in the docs means **Claude API integration**, not the Claude Code CLI — all three providers are plain API clients behind one interface. Image OCR is delegated to whichever LLM the tenant selected (no fixed OCR provider).

**Variable embedding dimensions are the sharp edge here.** pgvector stores mixed dimensions in one dimensionless `vector` column, but HNSW/IVFFlat indexes require uniform dimensions — so indexes are built per-dimension via expression + partial indexing. Indexes cap at 2,000 dimensions (`halfvec` extends to 4,000); models exceeding that (e.g. `text-embedding-3-large` at 3072) need dimension reduction or `halfvec`. Critically, **vectors from different models are not comparable** — store the generating model and dimension alongside each vector, and expect a full re-embed of a tenant's knowledge when its embedding model changes.

**Search is hybrid**: pgvector similarity + KB-API keyword. Build similarity first (it is the bulk of the new pipeline work); keyword search is largely the original memo's `glab` approach and is cheap to add later.

**Change detection is cron polling, never webhooks** — Redmine lacks native webhook support, so polling is the only mechanism that works across all three backends uniformly.

**Notifications**: label-applied-to-knowledge triggers delivery in-app, to Slack, and by email. Destination resolution is a label→destination mapping configured per tenant in admin settings — not KB assignees/watchers, since the auth model provides no KB-user-to-account linkage.

**Auth is deliberately minimal**: no custom auth mechanism; operation proceeds on admin-issued account identifiers, extensible later to password auth or corporate IdP (SAML/OIDC). ⚠️ **This assumes closed internal deployment.** If the system is ever exposed to the internet, this assumption breaks and real authentication becomes a prerequisite, not an enhancement.

**Browser storage**: localStorage holds account/session identity and a conversation-history *cache* only. The DB is authoritative for history so chats resume across devices. Credentials never reach the browser.

**Four UI surfaces**: account management (identity switching, read-only tenant display, minimal profile — no credential handling), chat (Claude-style, accepts pasted content, shows knowledge list and in-app notifications), admin settings (KB selection, LLM selection, label→notification mapping), developer screen (debugging/maintenance, tenant provisioning).

## Still open

Deliberately deferred until real data or operational experience exists: cron polling interval, pgvector index type (ivfflat vs hnsw), hybrid-search merge strategy (RRF etc.), and the re-embedding workflow when a tenant switches embedding model.

## Regenerating the overview PNG

No SVG renderer, `pip`, or `sudo` is available in this WSL environment. The PNG is produced through the Windows Chrome install via WSL interop:

```bash
"/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" --headless --disable-gpu \
  --screenshot="$(wslpath -w docs/system-overview.png)" --window-size=1240,1600 \
  "file:///$(wslpath -w docs/system-overview.svg | tr '\\' '/')"
```

Window size must match the SVG's `viewBox` (1240×1600) or the output is cropped or padded.

## Working notes

- `markitdown` output is explicitly optimized for text-analysis consumers rather than human-fidelity conversion — that matches the embedding-preprocessing use case, so do not treat its lossy formatting as a defect.
- Uploaded documents are untrusted input; markitdown's own docs call for sanitization.
