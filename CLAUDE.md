# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Implemented under `src/kb/`: the ingest pipeline (convert → chunk → embed → store), similarity search, and the GitHub knowledge-base adapter. Not yet built: GitLab/Redmine adapters, hybrid-result merging, notifications, UI, and real embedding providers.

```bash
uv run python -m pytest
```

Tests that need an external service skip themselves unless its env vars are set. Run them before trusting changes to `kb.db` or `kb.backends`:

```bash
KB_TEST_DSN="postgresql://postgres:devpass@localhost:55432/knowledge" KB_GITHUB_TEST_REPO=goodnasubi/kb-adapter-test KB_GITHUB_TOKEN="$(gh auth token)" uv run python -m pytest
```

`kb-adapter-test` is a private throwaway repo for adapter tests; the fixtures close the issues they create (GitHub has no delete). Mocked tests alone will not catch API drift — the live suite has already caught one divergence, so keep both.

`uv.lock` is committed, so `uv sync --extra dev` reproduces exact versions. Keep it that way — this is an application, not a distributed library, and unpinned transitive deps are how "works on my machine" starts.

The environment does not come ready: Ubuntu 20.04 ships Python 3.8 (markitdown needs 3.10+), has no `pip`, and `sudo` prompts for a password so it cannot be scripted. `uv` at `~/.local/bin` supplies both Python 3.12 and package management without any of that. Postgres runs through Docker Desktop's WSL integration. Setup notes and the standalone design check live in [verify/README.md](verify/README.md).

**Appending to knowledge means adding a comment, never editing the body** — concurrent body edits silently drop someone else's text, and all three backends have comments (GitLab notes, Redmine journal notes), so this is also the portable choice. `Knowledge.combined_text()` folds title + body + comments + attachment text into the string that gets embedded.

Two GitHub-specific traps, both confirmed against the live API: its issues endpoint **also returns pull requests** (filter on the `pull_request` key or PRs enter the knowledge base), and newly created issues take a few seconds to appear in the list endpoint. The latter is harmless in production — we embed our own writes immediately, and polling only catches external edits — but it will flake any test that lists right after creating.

No API keys exist here, so `HashingEmbeddingProvider` stands in for real providers. It is a bag-of-words hash, deliberately not a stub returning noise: texts sharing vocabulary land close together, which is what lets the end-to-end "find the similar document" test mean anything without a key. It does not model paraphrase, so never use it to judge retrieval quality.

## What's here

- [docs/knowledge-base-system-requirements.md](docs/knowledge-base-system-requirements.md) — **the source of truth.** A multi-backend, multi-tenant knowledge base system. Most design questions have now been resolved through a decision-by-decision review; each decision records its rationale, so read the reasoning before revisiting one.
- [docs/system-overview.html](docs/system-overview.html) — plain-language whole-system explainer for non-engineers. **This HTML is the source; `system-overview.png` is a render of it** — edit the HTML and re-render, never touch the PNG alone. It teaches the 本棚 (bookshelf) / 索引カード (index card) metaphor for the source-of-truth split below; reuse that vocabulary when explaining the system to non-technical readers.
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

**Vector index is HNSW, and the chunk table is partitioned by `tenant_id`.** Two non-obvious reasons, both worth preserving:

- IVFFlat cannot be created on an empty table (its k-means step needs rows) and its recall decays as rows are added past the initial clustering. Knowledge here starts empty per tenant and grows continuously, so IVFFlat fights the write pattern.
- With approximate indexes a `WHERE` clause is applied *after* the index scan. A tenant filter matching ~10% of rows leaves roughly 4 rows out of the default `hnsw.ef_search = 40`. Partitioning by tenant confines the scan to one tenant's index instead, which is why tenant isolation here is a partitioning decision, not a filtering one.

Long files are chunked, so one knowledge entry maps to many rows; collapse chunk hits to entry level with `MIN(distance)` after over-fetching.

Three behaviours here were verified against PostgreSQL 17 + pgvector 0.8.5 (`verify/pgvector-design.sql` reproduces all of them) and each fails *silently*:

- Index and query must use the **same cast expression** (`embedding::vector(768)`) or Postgres falls back to a sequential scan with no error.
- **`hnsw.ef_search` can cap how many rows a query returns**, so it bounds the over-fetch, not just recall — at the default 40, `LIMIT 100` may yield only 40 rows. Whether the cap binds depends on the data: dispersed vectors hit it, near-identical ones did not. Never count on exceeding `ef_search`; set it to at least the over-fetch size. Note `SET LOCAL` takes no parameters and does nothing under autocommit, so `ChunkRepository.search` uses `set_config(..., true)` inside an explicit transaction.
- A small table gets a `Seq Scan` even with a valid HNSW index — the planner's choice, not a defect. New tenants look "unindexed" until they accumulate rows; confirm with `SET enable_seqscan = off` before investigating.

**Change detection is cron polling, never webhooks** — Redmine lacks native webhook support, so polling is the only mechanism that works across all three backends uniformly. Interval is 10 minutes, and it belongs in config, not code. Note what that interval actually governs: knowledge written *through this system* is embedded immediately, so polling only catches edits made directly in the KB — a low-frequency event.

**Knowledge-to-knowledge links are a third ranked signal**, merged by the same RRF. They reach knowledge that neither vector nor keyword search can: search "ORA-01555" and the parent ticket "DB接続エラー全般の調査手順" surfaces even though that phrase never appears in it, because a person linked them. Links are KB-native (GitHub `#123` cross-references, GitLab related issues, Redmine issue relations) — no graph database or external library involved. `knowledge_edges` is derived data, partitioned by tenant, rebuildable from the KB like embeddings.

Two things about edges that are easy to get wrong, both found by tests:

- Store each edge **once, in the direction of whichever knowledge declared it**. Storing both directions means re-syncing one issue deletes edges the *other* issue declared, because the delete has to match "any edge touching me". Traversal reads both directions instead.
- GitHub's `cross-referenced` event fires only on the **referenced** side: writing `#B` in A's body puts the event on B's timeline, not A's. Nothing is lost — syncing every issue records it from B's side, and traversal is undirected — but `relations(A)` returning nothing there is correct, not a bug.

Default expansion is one hop; more pulls in weakly related knowledge. Damp the signal with an RRF weight rather than by cutting hops.

**Hybrid results merge with RRF**, on ranks alone. Weighted score fusion is not an option here: the two searches return incomparable values (cosine distance vs. KB relevance), some KB search APIs return no score at all, and the three backends define relevance differently. Ranks are the only signal all of them reliably produce.

**Switching a tenant's embedding model runs new and old vectors side by side**, then flips `embedding_model` in tenant config once every chunk is regenerated — searches keep serving from the old model until that moment, and rollback is one field. Delete the old rows afterward, then `REINDEX` before `VACUUM`. The trigger is manual and deliberately separate from changing the model setting, since a re-embed costs real money and time.

**Notifications**: label-applied-to-knowledge triggers delivery in-app, to Slack, and by email. Destination resolution is a label→destination mapping configured per tenant in admin settings — not KB assignees/watchers, since the auth model provides no KB-user-to-account linkage.

**Auth is deliberately minimal**: no custom auth mechanism; operation proceeds on admin-issued account identifiers, extensible later to password auth or corporate IdP (SAML/OIDC). ⚠️ **This assumes closed internal deployment.** If the system is ever exposed to the internet, this assumption breaks and real authentication becomes a prerequisite, not an enhancement.

**Browser storage**: localStorage holds account/session identity and a conversation-history *cache* only. The DB is authoritative for history so chats resume across devices. Credentials never reach the browser.

**Four UI surfaces**: account management (identity switching, read-only tenant display, minimal profile — no credential handling), chat (Claude-style, accepts pasted content, shows knowledge list and in-app notifications), admin settings (KB selection, LLM selection, label→notification mapping), developer screen (debugging/maintenance, tenant provisioning).

## Still open

Every design question is settled. What remains is numeric tuning against real data — the *approach* is fixed in each case, so do not reopen the decision when adjusting the number:

| Knob | Starting value | What tells you to change it |
|---|---|---|
| cron polling interval | 10 min | how often people actually edit in the KB directly |
| RRF constant `k` | 60 | whether similarity or keyword results should dominate |
| `hnsw.ef_search` | 40 (default) | recall vs. latency |

## Regenerating the overview PNG

No image library, SVG renderer, `pip`, or `sudo` is available in this WSL environment. The PNG is rendered from the HTML through the Windows Chrome install via WSL interop:

```bash
sed 's/<html lang="ja">/<html lang="ja" data-theme="light">/' docs/system-overview.html > /tmp/ov-light.html && "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" --headless --disable-gpu --screenshot="$(wslpath -w docs/system-overview.png)" --window-size=1048,2044 --hide-scrollbars --force-device-scale-factor=2 "file:///$(wslpath -w /tmp/ov-light.html | tr '\\' '/')"
```

Two things this command depends on:

- **Window height must equal the page's content height** or the PNG gets cut off / gains dead space. There is no full-page flag and no image library to trim with, so re-measure after editing the HTML: append a `load` handler that writes `document.documentElement.scrollHeight` onto `<body data-h>`, run Chrome with `--dump-dom`, and grep the value out.
- **Headless Chrome honors the OS dark-mode preference**, which produces a dark PNG. Render from a copy whose `<html>` carries `data-theme="light"` — the committed HTML deliberately omits it so the page still follows the reader's own theme.

## Working notes

- `markitdown` output is explicitly optimized for text-analysis consumers rather than human-fidelity conversion — that matches the embedding-preprocessing use case, so do not treat its lossy formatting as a defect.
- Uploaded documents are untrusted input; markitdown's own docs call for sanitization.
