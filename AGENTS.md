# AGENTS.md

Router for coding agents (Codex, Cursor, generic LLM tooling). Project-wide rules; mirrored to `CLAUDE.md`. When you change one router, update the other. Substantive rules live under `docs/`; this file does not duplicate them.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named `tracefold` that ingests social, news, macro, DEX/CEX market, and provider evidence, extracts crypto entities, scores and audits research signals, and serves results over HTTP / WebSocket / CLI to a React operator console. GMGN's anonymous public WebSocket is one source adapter, not the product boundary. One PostgreSQL store. See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`events`, `token_intents`, `token_intent_resolutions`, `asset_identity_*`, `market_ticks`, `enriched_events`, `news_items`, `macro_observations`) are the only business truth. Deterministic derived read models (`token_radar_current_rows`, `token_profile_current`, `market_tick_current`, `news_stories`) each have exactly one runtime writer and are rebuildable. Current read models use stable product/window keys, never run/generation/attempt/timestamp/UUID identity; unchanged projections write zero serving rows. Macro live evidence reads `macro_observations` directly through six descriptive lenses, while completed-session research is one immutable DeepAgents publication with durable runs/checkpoints; neither is a deterministic judgment projection. News is one public, non-personalized adaptation of pinned WorldMonitor: primary low-latency OpenNews facts and, when `news.rss_enabled` is explicitly enabled, 179 code-owned public RSS breadth/corroboration feeds enter one deterministic Story/selection state, one sealed half-hour Brief current/LKG singleton, and one News-owned durable outbound Story-push state machine. RSS defaults off. Search, Token Radar, and Token Case expose source facts and transparent deterministic factors without a model-derived product layer. Workers recover exclusively by re-reading PostgreSQL on bounded code-owned clocks; there is no database wake plane. Provider raw frames are inputs, not facts.

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
