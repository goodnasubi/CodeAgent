# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Implemented under `src/kb/`: ingest (convert → chunk → embed → store), hybrid search (similarity + keyword + relations, merged by RRF), notifications, cron-style sync and the resident scheduler that drives it, the HTTP API, and all four backend adapters. `web/` holds the React UI. All three LLM/embedding providers are wired up and verified against their live APIs: Gemini and OpenAI for both embedding and image OCR, Claude for image OCR only (`kb.providers.{gemini,openai,claude}`).

```bash
uv run python -m pytest
```

Tests that need an external service skip themselves unless its env vars are set. Run them before trusting changes to `kb.db` or `kb.backends`:

```bash
KB_TEST_DSN="postgresql://postgres:devpass@localhost:55432/knowledge" KB_GITHUB_TEST_REPO=goodnasubi/kb-adapter-test KB_GITHUB_TOKEN="$(gh auth token)" uv run python -m pytest
```

`uvicorn` without `--reload` will happily serve stale code after an edit — that cost a debugging round once. Use `--reload` when testing API changes by hand.

`kb-adapter-test` is a private throwaway repo for adapter tests; the fixtures close the issues they create (GitHub has no delete). Mocked tests alone will not catch API drift — the live suite has already caught one divergence, so keep both.

`uv.lock` is committed, so `uv sync --extra dev` reproduces exact versions. Keep it that way — this is an application, not a distributed library, and unpinned transitive deps are how "works on my machine" starts.

The environment does not come ready: Ubuntu 24.04 has no `pip`, marks its system Python as PEP 668 externally-managed, and `sudo` prompts for a password so it cannot be scripted. `uv` at `~/.local/bin` supplies both Python 3.12 and package management without any of that. Postgres runs through Docker Desktop's WSL integration. Setup notes and the standalone design check live in [verify/README.md](verify/README.md).

**Backends differ in what they can do, so `KnowledgeBase` carries `supports_keyword_search` / `supports_relations`.** Callers branch on those flags and merge whatever ranked lists they actually get — RRF works on ranks, so fewer signals still produce a valid ranking. Re:lation is the reason this exists: it is a shared support inbox, not an issue tracker, and its API has **neither keyword search (documented as 「キーワード検索はできません」) nor any ticket-to-ticket linking**. Similarity search still works because that runs entirely on our pgvector side, so Re:lation remains a usable knowledge base on one signal instead of three. `search()` there raises `UnsupportedOperation` rather than returning `[]`, so a caller that ignores the flag cannot mistake "unsupported" for "no hits"; `relations()` returns `[]` because having no links is a legitimate state.

Re:lation also has no create-a-ticket endpoint — `POST /records` **without** a `ticket_id` creates a ticket wrapping the memo, and with one appends to it. Labels are assigned by **id**, so the adapter resolves names through `GET /labels` and merges with existing ids (the field replaces rather than appends). Rate limit is 60 requests/minute, far tighter than the others; avoid N+1 fetches. **This adapter is mock-tested only** — no Re:lation account exists here. Every other backend had real divergences that only live tests caught, so treat it as unverified until `tests/test_backends_relation_live.py` exists.

**Redmine has no labels at all** — categories are single-valued, trackers are a type, neither models "several labels on one entry". The adapter maps labels onto a **multi-select custom field** (`Labels` by default, configurable), which needs a one-time setup on each Redmine instance; `verify/redmine-setup.rb` creates it. When the field is absent, reads return no labels and `add_labels` raises `LabelFieldMissing` rather than silently doing nothing. Since decision 12 routes notifications by label, an unconfigured Redmine means notifications never fire — hence the loud failure.

Two more Redmine traps, both covered by tests: its `journals` include entries that are pure property changes with empty `notes` (filter them or edit history gets embedded), and `updated_since` **must pass `status_id=*`** or the API returns only open issues — silently losing exactly the resolved tickets a knowledge base exists to surface.

**GitLab must keep working on old, Free, self-hosted instances** — that is the deployment target, not gitlab.com. Two consequences already handled in `kb.backends.gitlab`, both verified live:

- The related-issues API (`/links`) is a **Premium feature**. `relations()` treats 403/404 there as "feature absent" and falls back to GitLab's system notes, which record `mentioned in issue #N` on the referenced side whenever someone writes `#N`. System notes are ancient and tier-independent, so links stay discoverable everywhere. Cross-project mentions (`group/project#12`) are deliberately skipped — knowledge IDs are per-project `iid`s and the numbers would collide.
- GitLab exposes two IDs: `id` is instance-wide, `iid` is the per-project counter that users actually see in `#123`. Knowledge IDs use `iid`. (One asymmetry: creating a link needs the *numeric project id*; the URL-encoded path 404s. Reading is unaffected.)

Also GitLab-specific: notes include **system notes** ("added ~bug label"). Filter them out of `comments` or operating history gets embedded as knowledge.

**Appending to knowledge means adding a comment, never editing the body** — concurrent body edits silently drop someone else's text, and all three backends have comments (GitLab notes, Redmine journal notes), so this is also the portable choice. `Knowledge.combined_text()` folds title + body + comments + attachment text into the string that gets embedded.

Two GitHub-specific traps, both confirmed against the live API: its issues endpoint **also returns pull requests** (filter on the `pull_request` key or PRs enter the knowledge base), and newly created issues take a few seconds to appear in the list endpoint. The latter is harmless in production — we embed our own writes immediately, and polling only catches external edits — but it will flake any test that lists right after creating.

`HashingEmbeddingProvider` remains the default so the suite runs without a key. It is a bag-of-words hash, deliberately not a stub returning noise: texts sharing vocabulary land close together, which is what lets the end-to-end "find the similar document" test mean anything without a key. It does not model paraphrase, so never use it to judge retrieval quality.

**Three real providers exist: Gemini, OpenAI, and Claude** (`kb.providers.gemini` against `generativelanguage.googleapis.com` — Google AI Studio keys, not Vertex, so no GCP project or service account; `kb.providers.openai` against `api.openai.com`; `kb.providers.claude` against the Anthropic API). Per-tenant API keys live encrypted in `tenant_model_settings`, alongside the model selection.

**Claude has no embedding provider, and never will** — Anthropic does not offer an embeddings API. This is the reason the LLM and embedding dropdowns do not offer the same choices, and why `build_embedder` rejects `claude` with a distinct message rather than the generic "unsupported" one: it is a permanent absence, not an unimplemented feature. A tenant on Claude still needs Gemini or OpenAI for search.

**Claude is also the one provider that uses the vendor SDK** (`anthropic`) rather than `httpx`. Gemini and OpenAI are hand-rolled because two endpoints don't justify a dependency; Claude departs from that deliberately — Anthropic documents the SDK as the expected path, and the SDK already handles the retry/backoff and typed-error taxonomy this code would otherwise reimplement. Don't "fix" the inconsistency by rewriting it onto `httpx`.

Two Claude-specific traps, both pinned by tests:

- **The response is not always `content[0].text`.** Models that think emit a `thinking` block first (observed on `claude-sonnet-5`), so the text must be selected by block type. Reading index 0 silently yields empty output on exactly the models someone would upgrade to.
- **A refusal arrives as HTTP 200** with `stop_reason: "refusal"` and possibly empty content — check `stop_reason` before reading content, or a refused image looks like an image with no text in it. `stop_details` can be absent, so branch on `stop_reason` alone.

`thinking` is deliberately never sent: whether it can be set, and whether disabling it is legal, varies by model (some reject an explicit disable). Leaving it to the model's default works everywhere, and `max_tokens` is set high enough that thinking plus transcription both fit.

Three OpenAI-specific traps, all already handled:

- **`/embeddings` does not promise to return `data` in input order.** Each element carries an `index`; the provider reorders by it. Getting this wrong stores one knowledge's vector under another's ID — silently, and only visible as bad search results much later. `test_response_order_is_not_trusted` pins it.
- **429 means two different things.** A real rate limit, and `insufficient_quota` (no credits) — which is *not* transient. Telling someone to "wait and retry" when they need to buy credits sends them into an unbounded wait, so the provider branches on `error.type`. The live test skips (rather than fails) on the credit case, since billing state is not a code defect, but it says so loudly in the skip reason.
- **Its distances run much higher than Gemini's**, so the `max_distance` values are nowhere near each other (0.60 vs 0.40) despite both being 1,536-dimensional. Gemini's number applied to OpenAI drops 14 of 17 correct answers.

`gpt-5-mini` is the OCR default: correct on the test image, and mini-tier matters because this runs per uploaded image. It is slow though — 7.2s median versus 1.8s for `gpt-5.2` (both verified). The model is per-tenant configurable if that latency bites.

Three things it enforces that would otherwise fail late:

- **Output dimensions are capped at 2,000**, not the API's 3,072, because pgvector cannot build an HNSW index above that. Rejecting at construction beats discovering it when index creation fails after ingest. Default is 1,536.
- Vectors are always L2-normalized. Gemini normalizes at the default dimension but not always at reduced ones, and search is cosine distance.
- Empty strings never reach the API (they error and cost money); they get a deterministic unit vector, since a zero vector has no defined cosine distance.

**A model appearing in `ListModels` does not mean your key can call it.** `gemini-2.5-flash` is still listed but returns 404 "no longer available to new users" on `generateContent` for keys issued now — which is exactly what `tests/test_providers_gemini_live.py` exists to catch, since it deliberately exercises the *default* model names. Change a default, run the live test.

Its `max_distance` is **0.40**, measured — see the cutoff section below for why that number and not 0.85.

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

**Similarity search must cut off at `max_distance`.** Without it every query returns the top N no matter how far away they are — which floods results with noise *and* makes "nothing found, register it as new" unreachable, since the empty state never happens.

**The cutoff belongs to the embedding provider, not to a shared constant.** `EmbeddingProvider.max_distance` is part of the protocol and each provider declares its own; `HybridSearch` reads it from the embedder unless the caller overrides. This is not a style preference — the distance distribution *is* a property of the model, and one global number is wrong for every provider but one. `ChunkRepository.search` therefore defaults to **no cutoff**: it does not know whose vectors it holds, and a plausible-looking default there is how the wrong threshold spreads.

Note the three-way distinction in `HybridSearch(max_distance=...)`: omitted means "ask the provider" (`PROVIDER_DEFAULT`), `None` means "no cutoff" (for inspecting the distribution), and a number overrides. `None` could not do double duty, hence the sentinel.

Measured values, both by the same method — short query against full knowledge text, which is what search actually does:

| Provider | `max_distance` | Correct pairs | Queries matching nothing |
|---|---|---|---|
| `HashingEmbeddingProvider` | 0.85 | 0.46–0.65 | 0.88–1.00 |
| `gemini-embedding-001` (1,536d) | **0.40** | max 0.372 | min 0.401 |
| `text-embedding-3-small` (1,536d) | **0.60** | max 0.744 | min 0.550 |

For Gemini the usable band is narrow: 0.45 lets queries that match nothing start returning results (killing the register-new path), 0.35 starts dropping correct answers. Only 0.40 satisfies both. Unrelated *pairs* leaking in at 0.40 (4 of 55) is accepted — they rank below the correct hit and RRF orders the output; what must hold is that a query matching nothing comes back empty.

**Score thresholds by query, not by pair.** The question is whether a question that matches nothing comes back empty — not how many document-query pairs fall under the line. The two metrics disagree: for OpenAI, 0.62 and 0.60 retain identical recall, but per-query leakage differs (2/12 vs 1/12), which is the whole argument for picking 0.60.

**And use enough no-match queries.** OpenAI's threshold was first set to 0.62 off three of them, whose nearest-neighbour floor looked like 0.669. Widening to twelve dropped that floor to 0.550 and moved the answer. A gap in the distribution usually means a small sample, not a real gap.

**Do not tune any of this against `HashingEmbeddingProvider`** — it compares vocabulary overlap, not meaning, so its distances run high and mean something different. `tests/test_providers_gemini_live.py` asserts the separation against the live API, so model drift breaks the test rather than silently breaking search. If it fails, re-measure the distribution before touching the constant.

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

`HybridSearch` is where the three signals actually come together, and it is written to **degrade rather than fail**: a backend that lacks a capability is skipped via `supports_*` (never called), and a KB outage during keyword search is caught so similarity results still return — that half runs entirely on pgvector. Every skip is recorded in `SearchDiagnostics.skipped` with a reason, so "the graph added nothing" is distinguishable from "the graph was never consulted". **A signal that ran and returned zero rows must record a reason too** — keyword search originally left the entry empty on a no-match, which made a working search look identical to one that was never called.

Search results are enriched from `knowledge_index`, a per-knowledge metadata table written at ingest time — **not** by calling the KB per hit. N+1 fetches would break Re:lation's 60 requests/minute immediately, and the local copy keeps titles and URLs displayable while the KB is down.

`candidates_per_signal` (how many each signal contributes) is deliberately separate from `limit` (how many come back), and small values are **not** silently clamped up to `limit`.

**The API and the UI.** `kb.api` (FastAPI) is the only thing the browser talks to; `web/` is the React app, proxying `/api` in dev. Two rules the API enforces and must keep enforcing: **the KB token is never in a response** (`GET /kb` returns the connection without it), and `KB_SECRET_KEY` must be set or startup fails — a missing key that only surfaces when someone saves a token is worse than one that stops the process.

`GET /kb` also reports `supports_keyword_search` / `supports_relations` so the settings screen can tell the operator which searches their backend cannot do, rather than leaving them wondering why results look thin.

**Clicking a search hit opens the knowledge in the middle column, and that view is deliberately not part of the conversation.** Search results carry no body text — only `knowledge_index` metadata — so opening one fetches from the KB (`GET /knowledge/{id}`, a round trip that takes about a second and can fail; the view keeps the title, labels, and KB link when it does). The fetched body is never written into the conversation: the KB is the source of truth, so a copy in the chat log would just be stale duplication, and re-reading always re-fetches. Switching conversations or running a new search closes it.

**The Markdown renderer in `web/src/markdown.tsx` is hand-written, and should stay that way.** It covers what Issue bodies and LLM transcription actually produce — headings, lists, code, quotes, rules, bold, links, images. Three decisions in it are load-bearing:

- **It builds React elements, never HTML strings.** Bodies are external input and include generated text; constructing elements means React does the escaping and there is no `innerHTML` path to abuse. Link and image URLs are restricted to `http`/`https`.
- **Italics are not supported.** `_` and `*` spans wreck `UNDO_RETENTION` and `SELECT * FROM …`, which is exactly the content this KB holds. The false-positive cost beats the feature.
- **Nested lists and tables render as trees, flat lists don't.** The middle column is narrow enough that a real table's columns collapse past three or four; a tree puts the first cell as the node and the rest as `header: value` children, so width stops mattering. Nesting depth is read from the actual leading-space count — Markdown accepts 2 or 4 and writers mix them — and a `|` line only becomes a table when the next line is a separator row, or ordinary prose containing a pipe turns into one.

Images are fetched **by the browser, directly from the KB** — unlike the body, they carry no app token, so the viewer needs their own access. A failed load degrades to a link rather than a broken-image icon, which covers every cause (no permission, deleted attachment, blocked egress) without having to tell them apart. Displaying private-repo attachments reliably would need the API to proxy them with the tenant token, and that endpoint would need SSRF defenses before it could ship.

Frontend tests are `npm test` in `web/` (vitest). `markdown.tsx` is the only thing covered so far and it is the piece that most needs it — a hand-written parser whose regressions are silent. Rendering is asserted by turning components into HTML strings with `renderToStaticMarkup`, so there is no testing-library dependency; `jsdom` is configured only because `location` is needed to resolve relative URLs.

**Files and URLs enter through `POST /extract/file` and `/extract/url`, which convert and return text without writing anywhere.** That shape is deliberate: the same extracted text feeds *both* the search that runs first and the registration that happens only if nothing was found, so extraction cannot be folded into either one. The UI drops the result into the message box rather than holding it hidden, so the operator can see and edit what will actually be searched and stored.

Three constraints that shape it:

- **`file://` URLs read arbitrary server files** — markitdown will happily return `/etc/hostname`. `DocumentConverter.convert_url` allows only `http`/`https` (`ALLOWED_URL_SCHEMES`), and that check belongs on the converter, not the endpoint, so every caller inherits it.
- Extracted text is cut at `MAX_EXTRACTED_CHARS` (60,000) with a `truncated` flag, because **GitHub rejects issue bodies over 65,536 characters** — silently handing back text that cannot be registered is worse than saying it was shortened. Uploads cap at `MAX_SOURCE_BYTES` (20MB).
- **Image OCR runs through the tenant's LLM, so it works only where one is configured.** `DocumentConverter(llm=...)` takes the client from `llm_for_tenant`; with no key configured the argument is `None` and images come back with no text, which the UI says rather than failing silently (hence `has_llm_api_key` on the extract response). markitdown's own `llm_client` hook is deliberately unused — it calls OpenAI-shaped `chat.completions.create`, and its default prompt asks for a *description* of the image, which is the wrong job.

File upload needs `python-multipart`. On the frontend, `request()` must *not* set `Content-Type` for `FormData` — writing it by hand drops the multipart boundary and the server cannot parse the body.

Environment: `KB_DSN` (Postgres), `KB_SECRET_KEY` (Fernet key for token encryption), `KB_SYNC_INTERVAL_SECONDS` (scheduler interval, default 600), `KB_SMTP_HOST`/`KB_SMTP_PORT`/`KB_SMTP_SENDER` (optional, enables the email notifier), `KB_GEMINI_API_KEY`/`KB_OPENAI_API_KEY`/`KB_ANTHROPIC_API_KEY` (only for the live provider tests — the app itself reads per-tenant keys from the DB). `.env.example` is the template; `.env` is gitignored and nothing loads it automatically, so `set -a; . ./.env; set +a` before commands that need it. A malformed interval raises at startup rather than falling back to the default — silently ignoring the config is how a "why isn't it polling every 2 minutes" hunt starts. The dev frontend needs `npm install` in `web/`; `npm test` there runs the vitest suite. `package.json` used to override `rollup` to `@rollup/wasm-node` because Ubuntu 20.04's glibc 2.31 could not run rollup's native binary; the environment is Ubuntu 24.04 (glibc 2.39) now, so the override is gone. Restore it if this ever has to build on an older glibc.

`SyncRunner` is the polling entry point (fetch updates → read relations → embed and store → dispatch notifications). Three things there are deliberate:

- **The checkpoint timestamp is captured before fetching, not after.** Using the finish time would drop anything edited while the fetch was running.
- **The checkpoint only advances on a fully clean run.** A fetch failure or any per-item failure leaves it where it was, so the next poll retries that range. Re-ingesting already-stored knowledge is harmless — `replace_issue_chunks` replaces rows for the same model rather than appending.
- **A failure on one knowledge never aborts the batch**, and a `relations()` failure still ingests the knowledge itself: losing one search signal beats losing the knowledge.

First run starts from `EPOCH`, so onboarding a tenant imports everything the KB already holds. That is exactly why the notification dispatcher treats "no prior label state" as "notify nothing" — otherwise onboarding fires a notification per pre-existing label.

`kb.scheduler` is what actually calls `SyncRunner` on the interval — `python -m kb.scheduler`, a **separate process from the API**. Run it inside uvicorn and you get one scheduler per worker, all polling the same tenants. Four things there are load-bearing:

- **Concurrent syncs of one tenant double-fire notifications**, which is why `tenant_sync_lock` (a Postgres advisory lock keyed on the tenant) guards both the scheduler and the manual `POST /sync` — the endpoint returns 409 when the scheduler holds it, the scheduler records `"実行中"` and moves on. Re-ingesting is harmless (`replace_issue_chunks` overwrites), but label-diff detection reads-compares-writes `knowledge_label_state`, so two passes both see the label as newly added. A notification cannot be un-sent.
- **The loop swallows everything.** A per-tenant failure becomes a `skipped` entry; a whole-pass failure (DB unreachable) is logged and retried next interval. A resident process that exits on the first bad pass is worse than no scheduler at all. Correctness is preserved by `SyncRunner` not advancing its checkpoint.
- **A tenant with no KB configured is `skipped`, not failed** — provisioning a tenant before setting its backend is normal, and it must not look like breakage.
- **SIGTERM/SIGINT interrupt the wait, not a running pass.** Killing mid-pass just means the checkpoint doesn't advance and the next start redoes the range.

`build_sync_runner` in `kb.factory` assembles the runner for **both** the scheduler and the manual endpoint, so the two cannot drift into "notifications only fire from one of them". `build_shared_notifiers` is split out because `SlackNotifier` holds an `httpx.Client` — the resident process reuses one instead of building a client per tenant per pass. Email is registered only when `KB_SMTP_HOST` is set; without it, email-channel rules land in `DispatchResult.failed` as "未対応のチャネル" rather than vanishing.

**Hybrid results merge with RRF**, on ranks alone. Weighted score fusion is not an option here: the two searches return incomparable values (cosine distance vs. KB relevance), some KB search APIs return no score at all, and the three backends define relevance differently. Ranks are the only signal all of them reliably produce.

**Switching a tenant's embedding model runs new and old vectors side by side**, then flips `embedding_model` in tenant config once every chunk is regenerated — searches keep serving from the old model until that moment, and rollback is one field. Delete the old rows afterward, then `REINDEX` before `VACUUM`. The trigger is manual and deliberately separate from changing the model setting, since a re-embed costs real money and time.

**Notifications**: label-applied-to-knowledge triggers delivery in-app, to Slack, and by email. Destination resolution is a label→destination mapping configured per tenant in admin settings — not KB assignees/watchers, since the auth model provides no KB-user-to-account linkage.

Detecting "a label was applied" needs the previous labels, so `knowledge_label_state` stores what each sync last saw. Two behaviours there are deliberate and load-bearing:

- **A knowledge with no prior state notifies nothing.** `None` (never synced) and `()` (synced, no labels) are distinct — collapsing them would fire a notification for every pre-existing label the first time a tenant is onboarded.
- **State advances even when delivery fails.** Otherwise the 10-minute poll retries the same failed notification forever. Failures land in `DispatchResult.failed` for the caller; one broken channel never blocks the others.

`notification_rules` / `knowledge_label_state` / `app_notifications` are plain tables, not partitioned. Partitioning exists for the ANN filtering problem in `knowledge_chunks`; ordinary B-tree lookups don't have it.

**Auth is deliberately minimal**: no custom auth mechanism; operation proceeds on admin-issued account identifiers, extensible later to password auth or corporate IdP (SAML/OIDC). ⚠️ **This assumes closed internal deployment.** If the system is ever exposed to the internet, this assumption breaks and real authentication becomes a prerequisite, not an enhancement.

**Browser storage**: localStorage holds account/session identity and a conversation-history *cache* only. The DB is authoritative for history so chats resume across devices. Credentials never reach the browser.

**Four UI surfaces**: account management (identity switching, read-only tenant display, minimal profile — no credential handling), chat (Claude-style, accepts pasted content, shows knowledge list and in-app notifications, and opens a hit's full content in place of the thread), admin settings (KB selection, LLM selection, label→notification mapping), developer screen (debugging/maintenance, tenant provisioning).

## Still open

Every design question is settled. What remains is numeric tuning against real data — the *approach* is fixed in each case, so do not reopen the decision when adjusting the number:

| Knob | Starting value | What tells you to change it |
|---|---|---|
| cron polling interval | 10 min | how often people actually edit in the KB directly |
| RRF constant `k` | 60 | whether similarity or keyword results should dominate |
| `hnsw.ef_search` | 40 (default) | recall vs. latency |
| `max_distance` | per provider (Gemini 0.40, hashing 0.85) | irrelevant hits leaking in, or relevant ones being cut — **re-measure the distribution, and change it on the provider**, never as one shared number |

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
