# AGENTS.md

Router for coding agents (Codex, Cursor, generic LLM tooling). Project-wide rules; mirrored to `CLAUDE.md`. When you change one router, update the other. Substantive rules live under `docs/`; this file does not duplicate them.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named `tracefold` that turns provider news pushes and official macro data into audited research signals and serves them over HTTP / CLI to a React operator console (News + Macro). It is exactly two business capabilities — News V3 and Macro — over one PostgreSQL store; the former GMGN social/token/DEX/CEX market lane, Search, Token Case, and the live WebSocket were removed (#47, #49, #50). See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`news_items`, `macro_series_facts`, `macro_release_facts`, `macro_documents`, `macro_fed_official_role_facts`, `macro_document_analyses`, and Macro's general market observation facts `market_instruments`/`market_observations`/`market_settlements`/`market_position_facts`) are the only business truth. Deterministic derived read models (`news_events`, `macro_module_current`) each have exactly one runtime writer and are rebuildable. Current read models use stable product/window keys, never run/generation/attempt/timestamp/UUID identity; unchanged projections write zero serving rows. Macro live evidence is six deterministic typed module rows built from typed facts; optional immutable Fed document analyses (one structured LangChain call over the official body's evidence catalog) are supporting evidence and never gate official Rates/Fed current health. News V3 is one broker-driven Event pipeline: the authenticated OpenNews WSS pushes the account owner's `strategy.triggered` frames for the configured `news.opennews_strategy_ids` (validated against the provider Strategy list at startup; mismatches warn, never fail); a thin Receiver publishes each frame to RabbitMQ with publisher confirms and Tracefold sends no application subscription frame. RabbitMQ (`aio-pika`, quorum queues `news.raw`[SAC] / `news.triage` / `news.deliver`[SAC], two-level priority, one 30 s retry lane, one dead-letter queue) is the only transport/buffer/retry/concurrency plane for News; consumers handle up to `prefetch` messages concurrently, `TransientError` retries are counted (three, then dead-letter) while `DeferError` (the News DB lane could not admit the message) requeues uncounted; PostgreSQL holds facts, decisions, and audit, and every News write is idempotent by key (`news_items(item_id)`, `news_events(event_id)`, `news_verdicts(event_id, stage, policy_version)`, `news_deliveries(event_id, kind)`). The Deduper turns Items into Events (content-block title with pinned source-label normalization that keeps exchange names and @handles as subjects, exact fingerprint, MinHash/LSH near-duplicates, strong-fact veto, per-family windows), applies the deterministic Gate — evidence, not relevance: grounded assets are the provider's B+/A/A+ coin tags plus literal `$TICKER` cashtags with CL only in energy context, admission is `candidate` except recovery replays, deterministic listing frames, law-firm templates, and (behind `news.gate.suppress_low_signal`, default off) low-score ungrounded social posts, and a stronger later member re-gates a suppressed Event — and computes a theme-first preliminary storyline key. Triage is one structured LangChain call with a byte-frozen English system prompt that requires Chinese reader text (`headline_zh`, `why_zh`, console-only `title_zh`) and an end-of-message status bar (a fast retryable model failure earns one more attempt inside the deadline) and is the only semantic filter; the final storyline key is computed from the verdict's grounded primaries and written back; the pure `decide()` policy (`news.policy`: model push intent at magnitude >= 1, unclear-but-clear-event push, watchlist rescue, asset window-max throttle, theme cap per 4 h, hourly cap, control mutes — every path names its rule) owns the final decision, model failure is degraded but never silent (rule baseline: watchlist or score >= 80 with a grounded asset) with a circuit breaker, and the verdict carries `title_zh` and `audience` (no separate translation lane) plus a replayable trace (prompt sha, input sha, preliminary and final status-bar snapshots, final storyline key). There is no Analyst lane: one Event gets one structured judgment and one card (issue #57); `escalate` is a high-importance push (⚡ header, AMQP priority), not a second model call. The Deliverer performs at most one Feishu attempt per Event and renders the reader contract card (v8: `headline_zh` header, one `why_zh` sentence, direction/magnitude/tickers/source/time in plain words; no titles, enums, provider score, or AI label), drops instead of holding when delivery is paused, and a crash between send and ack terminalizes as ambiguous instead of resending. Control state (pause/mute) is a PostgreSQL singleton written by `tracefold news control` and read on every message; the Janitor republishes candidates that never left the process, expires bands, and snapshots broker depths. The learning plane is operator labels only (`tracefold news label` incl. `missed`, `news eval` over every Event, `news replay-decisions`, `news replay --gate-policy`, `news why <event_id>`); `/api/news/status.pipeline` reports where the last 24 h went (`suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`, `pushed_by_rule`, `labeled_missed_24h`, `candidate_share_24h`), `status.health`/`funnel_24h`/`reasons_24h` are the thresholded, Chinese-labelled view of the same facts, and every Event carries one server-owned `outcome` (`tracefold.news.outcome`, ten stable kinds with `text_zh`/`reason_zh`) shared by the feed, the detail timeline, and `news why` (issue #60); there is no market-mark or CEX price lane. Recovery of closed incidents uses the official Strategy hits endpoints and recovered Items never deliver. WorldMonitor RSS, Story, Brief, and pinned scoring are retired; deepagents is not a dependency. Macro workers recover exclusively by re-reading PostgreSQL on bounded code-owned clocks; News consumers recover by re-consuming durable broker queues plus database idempotency; there is no database wake plane. Provider raw frames are inputs, not facts.

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
