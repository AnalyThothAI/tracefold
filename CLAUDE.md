# CLAUDE.md

Claude-specific router. Mirrors `AGENTS.md` for the routing table and adds the Claude-only Skills / Plan-mode / Worktree protocol below. When you change either router, update the other.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named `tracefold` that ingests social, news, macro, DEX/CEX market, and provider evidence, extracts crypto entities, scores and audits research signals, and serves results over HTTP / WebSocket / CLI to a React operator console. GMGN's anonymous public WebSocket is one source adapter, not the product boundary. One PostgreSQL store. See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`events`, `token_intents`, `token_intent_resolutions`, `asset_identity_*`, `market_ticks`, `enriched_events`, `news_items`, `macro_series_facts`, `macro_release_facts`, `macro_documents`, `macro_fed_official_role_facts`, `macro_document_analyses`) are the only business truth. Deterministic derived read models (`token_radar_current`, `token_profile_current`, `market_tick_current`, `news_stories`, `macro_module_current`) each have exactly one runtime writer and are rebuildable. Current read models use stable product/window keys, never run/generation/attempt/timestamp/UUID identity; unchanged projections write zero serving rows. Macro live evidence is six deterministic typed module rows built from typed facts; optional immutable Fed document analyses are supporting evidence and never gate official Rates/Fed current health. News is one operator-bound, Strategy-qualified adaptation of pinned WorldMonitor: the authenticated OpenNews WSS automatically pushes the account owner's `strategy.triggered` events for the exact configured `news.opennews_strategy_ids`; Tracefold sends no application subscription frame. Each first accepted live OpenNews Item atomically creates its own durable Item Push admission when delivery is available; the first shared exact-atom identity inside the fixed provider-time window is the durable leader and later exact duplicates are terminal `suppressed`. Story/Feed/Brief remain independent asynchronous projections and Push never waits for or reads Story. Shared Item title presentation resolves independently before a leader's one fenced Feishu attempt, falls back to the visible original, and delivery never retries. Accepted multi-Strategy facts and, when `news.rss_enabled` is explicitly enabled, 179 code-owned public RSS breadth/corroboration feeds enter one Tracefold-owned Story V2 closure: the same exact comparison normalization, Jaccard as the only lexical score, strong typed fact compatibility, and deterministic fixed anchors. Public selection consumes that closure without reclustering, and one sealed half-hour Brief current/LKG singleton follows it. WorldMonitor remains pinned only for the exact RSS parser/catalog, classifier, selector, and Brief helpers documented in `docs/ARCHITECTURE.md`. Disconnects, process outages, and queue overflow create explicit incident intervals; the official authenticated Strategy list/hits endpoints provide bounded, idempotent gap recovery without using OpenNews Search, and incomplete provider retention remains visible as partial coverage. RSS defaults off. Token Radar is one fixed 4-hour change-first research queue: a 30-second deterministic reducer compares the current and prior adjacent four-hour windows from one bounded twelve-hour causal replay and atomically publishes at most fifty server-ordered evidence and market packets to one current/LKG singleton. It has no window query or control. Search, Token Radar, and Token Case expose source facts and transparent deterministic evidence without a model-derived product layer. Workers recover exclusively by re-reading PostgreSQL on bounded code-owned clocks; there is no database wake plane. Provider raw frames are inputs, not facts.

The current OpenNews cutover allowlist is exactly `1018` (News Score > 70) and
`1019` (OI Event Monitor). Provider-side Listing/Storage Strategies are
deliberately excluded; any future addition requires an explicit configuration
change.

## Agent skills

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for application/provider/credential/storage settings. Worker topology and safety/resource budgets are code-owned. Do not assume repository fixtures, example YAML, or `.env` files are the active runtime config. Before debugging provider data, Token Radar rows, asset profiles, or missing icons against real data, run `uv run tracefold config` and confirm the reported `config_path` points at `~/.tracefold/config.yaml`. Never print or copy secret values; report only redacted booleans, paths, and diagnostic command results.

## Frontend guardrails

Frontend CSS is harness-constrained, not convention-only. Before changing `web/src` UI code, read `docs/FRONTEND.md`. Do not recreate retired CSS buckets such as `cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, or `signalLab.css`; owner CSS must live beside the component or route that imports it. Feature CSS must use the owning feature namespace and must not restyle shared UI internals or Obsidian `.ods-*` selectors. `npm run lint` runs ESLint plus the frontend architecture harness; do not bypass it after CSS, responsive, route shell, or shared UI changes.

## Where to read what

| Need | File |
|------|------|
| Install, run, docker | `docs/SETUP.md` |
| Layer boundaries & data flow | `docs/ARCHITECTURE.md` |
| Frontend architecture | `docs/FRONTEND.md` |
| Public surfaces (config, WS, HTTP, CLI) | `docs/CONTRACTS.md` |
| Development, issue specs, design, testing | `docs/DEVELOPMENT.md` |
| Secrets, config, authn changes | `docs/SECURITY.md` |
| Operations, workers, PostgreSQL diagnosis | `docs/OPERATIONS.md` |
| Business package boundaries | `docs/ARCHITECTURE.md`; public Python interfaces are the `tracefold.market`, `tracefold.news`, and `tracefold.macro` package roots |
| Durable specs and acceptance | GitHub Issues; repository conventions are in `docs/agents/issue-tracker.md` |
| Auto-generated artefacts | `docs/generated/` |

CLI surface: `uv run tracefold --help` is the source of truth (snapshot at `docs/generated/cli-help.md`).

<!-- END SHARED AGENT ROUTER -->
