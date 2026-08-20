# CLAUDE.md

Claude-specific router. Mirrors `AGENTS.md` for the routing table and adds the Claude-only Skills / Plan-mode / Worktree protocol below. When you change either router, update the other.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named `tracefold` that turns provider news pushes into audited research signals and serves them over HTTP / CLI to a React operator console. It is exactly one business capability — News V3 — over one PostgreSQL store; the former GMGN social/token/DEX/CEX market lane, Search, Token Case, and the live WebSocket were removed (#47, #49, #50), and the Macro product line (six current modules, official-source acquisition, Fed document analyses, and the whole projection/EDF plane) was removed in #68. See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`news_items`) are the only business truth. The deterministic derived read model (`news_events`) has exactly one runtime writer and is rebuildable. Current read models use stable product/window keys, never run/generation/attempt/timestamp/UUID identity. News V3 is one broker-driven Event pipeline: the authenticated OpenNews WSS pushes the account owner's `strategy.triggered` frames for the configured `news.opennews_strategy_ids` (validated against the provider Strategy list at startup; mismatches warn, never fail); a thin Receiver publishes each frame to RabbitMQ with publisher confirms and Tracefold sends no application subscription frame. RabbitMQ (`aio-pika`, quorum queues `news.raw`[SAC] / `news.triage` / `news.deliver`[SAC], two-level priority, one 30 s retry lane, one dead-letter queue) is the only transport/buffer/retry/concurrency plane for News; consumers handle up to `prefetch` messages concurrently, `TransientError` retries are counted (three, then dead-letter) while `DeferError` (the News DB lane could not admit the message) requeues uncounted; PostgreSQL holds facts, decisions, and audit, and every News write is idempotent by key (`news_items(item_id)`, `news_events(event_id)`, `news_verdicts(event_id, stage, policy_version)`, `news_deliveries(event_id, kind)`). The Deduper turns Items into Events (content-block title with pinned source-label normalization that keeps exchange names and @handles as subjects, exact fingerprint, MinHash/LSH near-duplicates, strong-fact veto, per-family windows), applies the deterministic Gate — evidence, not relevance: grounded assets are the provider's B+/A/A+ coin tags plus literal `$TICKER` cashtags with CL only in energy context, admission is `candidate` except recovery replays, law-firm templates, and (behind `news.gate.suppress_low_signal`, default off) low-score ungrounded social posts; exchange listing/delisting frames take the `listing_deterministic` admission, which is admitted and goes to Triage like a candidate (#72 — it used to be stored and then silently dropped), and a stronger later member re-gates a suppressed Event — and computes a theme-first preliminary storyline key. Triage is one structured LangChain call with a byte-frozen English system prompt that requires Chinese reader text (`headline_zh`, `why_zh`, console-only `title_zh` whose empty value means "same as `headline_zh`", #101) and an end-of-message status bar — window counts plus the told ledger, the cards the reader received in the last 4 h (issue #61) — (a fast retryable model failure or an unusable non-truncated answer earns one more attempt inside the deadline) and is the only semantic filter; the verdict carries `novelty` (`new_fact`/`progression`/`restatement` + `restates` index against the ledger); the final storyline key is computed from the verdict's grounded primaries, then a theme, then the model's own primaries even when the provider did not tag them, then a grounded tag the text actually names as its own token, and written back (#100: the old "any grounded tag" fallback put 16% of a day's asset-keyed cards in a bucket that was not about them — every OKX listing notice in `asset:OKB`, Polish jets scrambling in `asset:BTC`); the pure `decide()` policy (`news.policy`: grounded restatement drop, model push intent at magnitude >= 1, unclear-but-clear-event push, watchlist rescue, asset window-max throttle, theme cap per 4 h, hourly cap, control mutes — every path names its rule) owns the final decision, and since policy v5 (#81) the storyline throttle stops counting and starts reading: a throttled card is released as `distinct_bypass` when its `headline_zh` resembles nothing the reader received in the window (character-bigram Jaccard under `news.policy.similarity_max`), is withheld with a `:seen` key when it does, and the counts survive only as a flood ceiling (`distinct_hard_cap_4h` / `distinct_asset_cap_2h`) — this retired `novel_bypass`, the last path where the model's own unverified claim about itself opened a gate, and on the 08-18/20 corpus it cut facts the reader never received by 63% and near-duplicate pairs by 46% at once; policy v6 (#100, `news.policy.similarity_all_pushes`) then moved that measurement out from under the count throttle, because the count rule is per-storyline and a fresh key skipped it entirely — 55% of a live day's pushes were never compared with the ledger already in memory, one provider batch shipped 19 near-identical cards across 19 keys in 7 minutes, and replaying the frozen day cut near-duplicate pairs 104 -> 23 and facts the reader never received 39 -> 32 at once (two guards keep the metric honest: `escalate` stays on the v5 path, and a card whose direction contradicts the ledger entry it matched is never withheld — bigrams are blind to negation). `decide()` owns the final decision, taken and persisted inside one transaction under a per-storyline advisory lock (a card landing while the model was thinking earns one re-ask with the fresh ledger), model failure is degraded but never silent (rule baseline: watchlist, score >= 80 with a grounded asset, or a high-priority Event or deterministic exchange notice, which fail open onto the wire headline instead of dropping a missile strike because it has no ticker) with a circuit breaker, and the verdict carries `title_zh` and `audience` (no separate translation lane) plus a replayable trace (prompt sha, input sha, preliminary key, preliminary and final status-bar snapshots, the told ledger as shown, final storyline key). There is no Analyst lane: one Event gets one structured judgment and one card (issue #57); `escalate` is a high-importance push (⚡ header, AMQP priority), not a second model call, and since policy v4 (#77) it is magnitude-driven only — the Gate's `priority` still orders the queue but no longer decides the ⚡ header, so an exchange listing notice is not as loud as a missile strike (`news.policy.high_priority_escalates` restores the old behaviour). The Deliverer performs at most one Feishu attempt per Event and renders the reader contract card (v8: `headline_zh` header, one `why_zh` sentence, direction/magnitude/tickers/source/time in plain words; no titles, enums, provider score, or AI label), drops instead of holding when delivery is paused, and a crash between send and ack terminalizes as ambiguous instead of resending. Control state (pause/mute) is a PostgreSQL singleton written by `tracefold news control` and read on every message; the Janitor republishes candidates that never left the process, expires bands, and snapshots broker depths. The instrument universe (`news_market_instruments` + `news_symbol_aliases`, #75, consolidated in #89) is a rebuildable provider fact with exactly two jobs: it normalizes the storyline throttle key so one issuer's several contracts (SKHY/SKHX/SKHYNIX) share a bucket, and it tells the Gate whether a headline is about a coin or a stock. A bounded snapshot loop reads the Binance spot/USD-M and Hyperliquid perp/spot/HIP-3 builder-DEX catalogues (no credentials; a venue that fails to answer is skipped, never a mass delisting) and keeps the class the venue itself declares — Binance labels its 169 TradFi perps `EQUITY`/`CN_EQUITY`/`HK_EQUITY`/`KR_EQUITY`/`COMMODITY`/`PREMARKET`, and ignoring that field had put 81 of them in the universe as `crypto`; `classify()` is the fallback for venues (Hyperliquid) that declare nothing. The same loop reads one *reference* tier (`us.listed`, #91): the ~13k Nasdaq/NYSE tickers from the Nasdaq Trader symbol directory, which exist only to tell the Gate that `UWMC` or `TLX` is a stock — 133 Events in a week were grounded on an equity no crypto venue lists and read as crypto. It never overrides a traded symbol (352 crypto base symbols are also US tickers: `ATOM` is Atomera, `BCH` is Banco de Chile), it is excluded from `asset_refs`, the console's `符号落表` funnel and the `在交易合约` figure, and index membership was measured and rejected as a source (S&P 500 covers 4.6% of the residue — the large caps already have Binance TradFi perps). Code-owned seed aliases are reconciled into the table on every snapshot — the code wins, and a seed pointing at a symbol no venue lists is reported (`status.instruments.dangling_aliases`) instead of resolving to nothing forever. It is deliberately neither a filter nor a source of listing events: #75's existence whitelist was retired after a dry-run showed it only ever removed real equities with no crypto perp, and listing/delisting facts arrive as provider frames (#72) that the snapshot diff could only have duplicated for the two venues we poll. The learning plane is operator labels plus one release gate (`tracefold news label` incl. `missed` / `must_push`, correctable and able to record a miss with no Event; `news eval` over every Event; `news replay-decisions` for a first-order policy replay; `news corpus freeze` + `news validate-candidate`, which replays the deployed policy and a candidate *sequentially* over a frozen corpus and refuses to ship a candidate that loses a `must_push` case, grows misses, or buys recall with repetition; `news replay --gate-policy`; `news instruments unmatched`, the provider tags the universe cannot name; `news why <event_id>`). `news.retention` keeps raw Items 30 days and judged/labelled ones 365, because deleting `news_items` cascades to every verdict, delivery and operator label; `/api/news/status.pipeline` reports where the last 24 h went (`suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`, `pushed_by_rule`, `labeled_missed_24h` (both label shapes) and `labeled_missed_without_event_24h` — the misses the pipeline never created an Event for, which is the only observation of recall's upper bound — `candidate_share_24h`), `status.health`/`funnel_24h`/`reasons_24h` are the thresholded, Chinese-labelled view of the same facts, and every Event carries one server-owned `outcome` (`tracefold.news.outcome`, ten stable kinds with `text_zh`/`reason_zh`) shared by the feed, the detail timeline, and `news why` (issue #60). The Price Review plane (#88) is the third consumer of the instrument universe and the only place a price exists: two cold Workers loops, sharing one exact-symbol-first resolution strategy (an alias is a fallback, never a substitute — `SKHX` prices SKHX even though the throttle buckets it under `SKHY` — and `us.listed` is never a price candidate), write two derived read models with deliberately different lifecycles. `news_quote_snapshots` is latest-only current display state: every 5 s the planner deduplicates recent live Events plus the watchlist into unique Price Instruments (bounded at 256 across at most 12 source groups), issues at most one batch REST request per source (Binance spot/USD-M ticker, Hyperliquid `metaAndAssetCtxs` / `spotMetaAndAssetCtxs`, one per active HIP-3 dex; no credentials, concurrency 4, a 10 s turn deadline, turns never overlapping), and replaces one row per source — so a hundred Events naming BTC are one target and one provider result, and a venue that fails leaves its previous row to age into `stale` rather than blanking to zero. `news_event_reactions` is the deterministic answer to "what did the market do after this": a 60 s loop scans at most 100 due Event-assets over *every* live grounded Event (held ones included — that is what the miss review needs), merges their candle ranges into at most 32 requests, takes `p0` as the last closed 5 m candle at or before `opened_at_ms` (the provider publication time, never delivery) and `p1`/`p4` the same at +1H/+4H, and stores `(pH/p0)-1` as integer basis points keyed by `(event_id, symbol, metric_version)`; `reaction_v1` freezes interval, alignment, gap tolerance, source selection, aggregation and the hit definition, a gap is `no_candle_within_gap` and never forward-filled, and a transient provider failure writes nothing so the work stays due. `/api/news/quotes` (≤100 symbols, `fresh|stale|unavailable|unlisted`) is deliberately not a feed field — a price that changed must not invalidate the Feed ETag every 3 s — while `/api/news/review` serves 命中复盘: coverage before accuracy, every percentage paired with its N, neutral/unclear reported outside the hit denominator, and withheld Events ranked by absolute 1H move as a *queue*, not a verdict (movement proves no causality, and nothing there writes a label). Price never reaches the Gate, Triage, `decide()`, a card, a throttle key or a ranking signal, the loops admit their DB work through a one-slot cold lane rather than the four News hot slots, and there is still no market-mark lane, tick history, OI, order book, or market socket. Recovery of closed incidents uses the official Strategy hits endpoints and recovered Items never deliver. WorldMonitor RSS, Story, Brief, and pinned scoring are retired; deepagents is not a dependency. News consumers recover by re-consuming durable broker queues plus database idempotency; there is no database wake plane, no projection/EDF frontier, and no durable queue terminal-evidence lane. Provider raw frames are inputs, not facts.

The News allowlist is operator-owned in `news.opennews_strategy_ids` (currently `1018` News Score > 70, `1352` Storage News, `1353` Listing and Delisting Announcements); provider-side `1019` OI Event Monitor is disabled and not configured. Workers compare the configured list with the provider Strategy list at startup and expose warnings in `/api/news/status`; any change is an explicit configuration change.

## Agent skills

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for application/provider/credential/storage settings. Worker topology and safety/resource budgets are code-owned. Do not assume repository fixtures, example YAML, or `.env` files are the active runtime config. Before debugging provider data or News events against real data, run `uv run tracefold config` and confirm the reported `config_path` points at `~/.tracefold/config.yaml`. Never print or copy secret values; report only redacted booleans, paths, and diagnostic command results.

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
| Business package boundaries | `docs/ARCHITECTURE.md`; the public Python interface is the `tracefold.news` package root |
| Durable specs and acceptance | GitHub Issues; repository conventions are in `docs/agents/issue-tracker.md` |
| Auto-generated artefacts | `docs/generated/` |

CLI surface: `uv run tracefold --help` is the source of truth (snapshot at `docs/generated/cli-help.md`).

<!-- END SHARED AGENT ROUTER -->

## Claude-only protocol: worktrees, plan mode, skills

### Worktree by default (do not switch the primary checkout)

The primary checkout (`~/Documents/Code/tracefold`) is the deployment
checkout: it stays on `main`, stays clean, and is the only place `make up` /
`make status` / `make logs` run. Never `git checkout <branch>` there, never
edit files there for a task, and never leave it dirty. Every code or docs
change — including one-line fixes — starts in its own worktree so several
tasks (and several sessions) can proceed at the same time:

```bash
git fetch origin main
git worktree add -b claude/<issue-or-slug> .claude/worktrees/<slug> origin/main
cd .claude/worktrees/<slug> && uv sync && (cd web && npm ci)   # per-worktree venv/node_modules
```

- `EnterWorktree` (Claude Code tool) is the same thing: it creates
  `.claude/worktrees/<name>` from `origin/main` and moves the session there.
  Sibling directories (`../tracefold-<slug>`, the historical `codex/*`
  worktrees) are also fine; `.worktrees/` and `.claude/worktrees/` are
  gitignored.
- Branch from `origin/main`, not from local `main`; name branches
  `claude/<issue>-<slug>` (or the agent's own prefix). One task, one worktree,
  one branch, one PR.
- Before editing, run `git worktree list` and `git status` — an existing
  worktree belongs to its current task; do not reuse or clean it up.
- Tests run inside the worktree (`make test`, focused lanes from
  `docs/DEVELOPMENT.md`). The compose stack (`make up`, ports 8765/8766/5672,
  the `tracefold-*` volumes) is shared and owned by the primary checkout: do
  not run `make up`/`make down` from a worktree.
- Finish: PR → merge → in the primary checkout `git pull --ff-only origin main`
  → `make up` (deployment) → `git worktree remove .claude/worktrees/<slug>`
  and `git branch -d claude/<slug>`; `git worktree prune` for stale entries.
- Commit only on the task branch; never commit or push on `main` directly.

### Plan mode and skills

- Non-trivial changes (new table/worker/queue/prompt/policy, hard cuts) start
  with the GitHub issue (`docs/agents/issue-tracker.md`) and a plan; use plan
  mode to agree scope before editing.
- Run `/code-review` on the branch before opening the PR; `/security-review`
  when config, auth, credentials, or delivery code changes.
- Live-data debugging follows "Runtime config for real data" above: read-only
  SQL as the `tracefold_serve` role, `uv run tracefold config`, never print
  secrets.
