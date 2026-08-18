# CLAUDE.md

Claude-specific router. Mirrors `AGENTS.md` for the routing table and adds the Claude-only Skills / Plan-mode / Worktree protocol below. When you change either router, update the other.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named `tracefold` that turns provider news pushes and official macro data into audited research signals and serves them over HTTP / CLI to a React operator console (News + Macro). It is exactly two business capabilities — News V3 and Macro — over one PostgreSQL store; the former GMGN social/token/DEX/CEX market lane, Search, Token Case, and the live WebSocket were removed (#47, #49, #50). See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`news_items`, `macro_series_facts`, `macro_release_facts`, `macro_documents`, `macro_fed_official_role_facts`, `macro_document_analyses`, and Macro's general market observation facts `market_instruments`/`market_observations`/`market_settlements`/`market_position_facts`) are the only business truth. Deterministic derived read models (`news_events`, `macro_module_current`) each have exactly one runtime writer and are rebuildable. Current read models use stable product/window keys, never run/generation/attempt/timestamp/UUID identity; unchanged projections write zero serving rows. Macro live evidence is six deterministic typed module rows built from typed facts; optional immutable Fed document analyses (one structured LangChain call over the official body's evidence catalog) are supporting evidence and never gate official Rates/Fed current health. News V3 is one broker-driven Event pipeline: the authenticated OpenNews WSS pushes the account owner's `strategy.triggered` frames for the configured `news.opennews_strategy_ids` (validated against the provider Strategy list at startup; mismatches warn, never fail); a thin Receiver publishes each frame to RabbitMQ with publisher confirms and Tracefold sends no application subscription frame. RabbitMQ (`aio-pika`, quorum queues `news.raw`[SAC] / `news.triage` / `news.deep` / `news.deliver`[SAC], two-level priority, one 30 s retry lane, one dead-letter queue) is the only transport/buffer/retry/concurrency plane for News; consumers handle up to `prefetch` messages concurrently, `TransientError` retries are counted (three, then dead-letter) while `DeferError` (the News DB lane could not admit the message) requeues uncounted; PostgreSQL holds facts, decisions, and audit, and every News write is idempotent by key (`news_items(item_id)`, `news_events(event_id)`, `news_verdicts(event_id, stage, policy_version)`, `news_deliveries(event_id, kind)`). The Deduper turns Items into Events (content-block title, pinned normalization, exact fingerprint, MinHash/LSH near-duplicates, strong-fact veto, per-family windows), applies the deterministic Gate (engine type, asset class, grounded assets with CL only in energy context, macro lexicon, PR-template suppression) and computes a storyline key. Triage is one structured LangChain call with a byte-frozen system prompt and an end-of-message status bar (a fast retryable model failure earns one more attempt inside the deadline); the pure `decide()` rules (magnitude/direction thresholds, watchlist, storyline window-max throttle, hourly cap, control mutes) own the final decision, model failure is fail-closed with a circuit breaker, and the verdict carries `title_zh` (no separate translation lane) plus a replayable trace (prompt sha, input sha, status-bar snapshot). The Analyst is one structured LangChain call over a code-prefetched evidence bundle (event, members, storyline history, prior verdicts, macro state, status bar) whose evidence ids are registered by the host; the deterministic `verify_verdict()` gate rejects unknown evidence with one bounded correction round, a newer push in the same storyline supersedes the follow-up at the safe point, and it never blocks a push. The Deliverer performs at most one Feishu attempt per (Event, kind), renders code facts as the card body with sanitized AI copy, drops instead of holding when delivery is paused, and a crash between send and ack terminalizes as ambiguous instead of resending. Control state (pause/mute) is a PostgreSQL singleton written by `tracefold news control` and read on every message; the Janitor republishes candidates that never left the process, expires bands, and snapshots broker depths. The learning plane is operator labels only (`tracefold news label`, `news eval`, `news replay-decisions`); there is no market-mark or CEX price lane. Recovery of closed incidents uses the official Strategy hits endpoints and recovered Items never deliver. WorldMonitor RSS, Story, Brief, and pinned scoring are retired; deepagents is not a dependency. Macro workers recover exclusively by re-reading PostgreSQL on bounded code-owned clocks; News consumers recover by re-consuming durable broker queues plus database idempotency; there is no database wake plane. Provider raw frames are inputs, not facts.

The News allowlist is operator-owned in `news.opennews_strategy_ids` (currently `1018` News Score > 70, `1352` Storage News, `1353` Listing and Delisting Announcements); provider-side `1019` OI Event Monitor is disabled and not configured. Workers compare the configured list with the provider Strategy list at startup and expose warnings in `/api/news/status`; any change is an explicit configuration change.

## Agent skills

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for application/provider/credential/storage settings. Worker topology and safety/resource budgets are code-owned. Do not assume repository fixtures, example YAML, or `.env` files are the active runtime config. Before debugging provider data, News events, or Macro modules against real data, run `uv run tracefold config` and confirm the reported `config_path` points at `~/.tracefold/config.yaml`. Never print or copy secret values; report only redacted booleans, paths, and diagnostic command results.

## Frontend guardrails

Frontend CSS is harness-constrained, not convention-only. Before changing `web/src` UI code, read `docs/FRONTEND.md`. Do not recreate retired CSS buckets such as `cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, or `signalLab.css`; owner CSS must live beside the component or route that imports it. Feature CSS must use the owning feature namespace and must not restyle shared UI internals or Obsidian `.ods-*` selectors. `npm run lint` runs ESLint plus the frontend architecture harness; do not bypass it after CSS, responsive, route shell, or shared UI changes.

## Where to read what

| Need | File |
|------|------|
| Install, run, docker | `docs/SETUP.md` |
| Layer boundaries & data flow | `docs/ARCHITECTURE.md` |
| Frontend architecture | `docs/FRONTEND.md` |
| Public surfaces (config, HTTP, CLI) | `docs/CONTRACTS.md` |
| Development, issue specs, design, testing | `docs/DEVELOPMENT.md` |
| Secrets, config, authn changes | `docs/SECURITY.md` |
| Operations, workers, PostgreSQL diagnosis | `docs/OPERATIONS.md` |
| Business package boundaries | `docs/ARCHITECTURE.md`; public Python interfaces are the `tracefold.news` and `tracefold.macro` package roots |
| Durable specs and acceptance | GitHub Issues; repository conventions are in `docs/agents/issue-tracker.md` |
| Auto-generated artefacts | `docs/generated/` |

CLI surface: `uv run tracefold --help` is the source of truth (snapshot at `docs/generated/cli-help.md`).

<!-- END SHARED AGENT ROUTER -->
