# CLAUDE.md

Claude-specific router. Mirrors `AGENTS.md` for the routing table and adds the Claude-only Skills / Plan-mode / Worktree protocol below. When you change either router, update the other.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named `tracefold` that ingests social, news, macro, DEX/CEX market, and provider evidence, extracts crypto entities, scores and audits research signals, and serves results over HTTP / WebSocket / CLI to a React operator console. GMGN's anonymous public WebSocket is one source adapter, not the product boundary. One PostgreSQL store. See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`events`, `token_intents`, `token_intent_resolutions`, `asset_identity_*`, `market_ticks`, `enriched_events`, `news_items`, `macro_series_facts`, `macro_release_facts`, `macro_documents`, `macro_fed_official_role_facts`, `macro_document_analyses`) are the only business truth. Deterministic derived read models (`token_profile_current`, `market_tick_current`, `news_events`, `macro_module_current`) each have exactly one runtime writer and are rebuildable. Current read models use stable product/window keys, never run/generation/attempt/timestamp/UUID identity; unchanged projections write zero serving rows. Macro live evidence is six deterministic typed module rows built from typed facts; optional immutable Fed document analyses are supporting evidence and never gate official Rates/Fed current health. News V3 is one broker-driven Event pipeline: the authenticated OpenNews WSS pushes the account owner's `strategy.triggered` frames for the configured `news.opennews_strategy_ids` (validated against the provider Strategy list at startup; mismatches warn, never fail); a thin Receiver publishes each frame to RabbitMQ with publisher confirms and Tracefold sends no application subscription frame. RabbitMQ (`aio-pika`, quorum queues, single-active-consumer for the Deduper and Deliverer, two-level priority, TTL retry lanes, one dead-letter queue, a fanout control exchange) is the only transport/buffer/retry/concurrency plane for News; PostgreSQL holds facts, decisions, and audit, and every News write is idempotent by key (`news_items(item_id)`, `news_events(event_id)`, `news_verdicts(event_id, stage, policy_version)`, `news_deliveries(event_id, kind)`, `news_title_presentations(comparison_fingerprint)`). The Deduper turns Items into Events (content-block title, pinned normalization, exact fingerprint, MinHash/LSH near-duplicates, strong-fact veto, per-family windows), applies the deterministic Gate (engine type, asset class, grounded assets with CL only in energy context, macro lexicon, PR-template suppression) and computes a storyline key. Triage is one structured LangChain call with a byte-frozen system prompt and an end-of-message status bar; the pure `decide()` rules (magnitude/direction thresholds, watchlist, storyline window-max throttle, hourly cap, control mutes) own the final decision, and model failure is fail-closed with a circuit breaker. The Analyst is a minimal `deepagents` harness (seven read-only tools called concurrently in one turn, no subagents, no checkpointer, structured terminal output, deterministic `verify_verdict()` evidence gate) that never blocks a push and only produces follow-up cards. The Deliverer performs at most one Feishu attempt per (Event, kind), renders code facts as the card body with sanitized AI copy, and a crash between send and ack terminalizes as ambiguous instead of resending. Recovery of closed incidents uses the official Strategy hits endpoints and recovered Items never deliver. WorldMonitor RSS, Story, Brief, and pinned scoring are retired. Search and Token Case expose source facts and transparent deterministic evidence without a model-derived product layer. Market and Macro workers recover exclusively by re-reading PostgreSQL on bounded code-owned clocks; News consumers recover by re-consuming durable broker queues plus database idempotency; there is no database wake plane. Provider raw frames are inputs, not facts.

The News allowlist is operator-owned in `news.opennews_strategy_ids` (currently `1018` News Score > 70, `1352` Storage News, `1353` Listing and Delisting Announcements); provider-side `1019` OI Event Monitor is disabled and not configured. Workers compare the configured list with the provider Strategy list at startup and expose warnings in `/api/news/status`; any change is an explicit configuration change.

## Agent skills

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for application/provider/credential/storage settings. Worker topology and safety/resource budgets are code-owned. Do not assume repository fixtures, example YAML, or `.env` files are the active runtime config. Before debugging provider data, News events, asset profiles, or missing icons against real data, run `uv run tracefold config` and confirm the reported `config_path` points at `~/.tracefold/config.yaml`. Never print or copy secret values; report only redacted booleans, paths, and diagnostic command results.

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
