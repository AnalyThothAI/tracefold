# AGENTS.md

Router for coding agents (Codex, Cursor, generic LLM tooling). Project-wide
rules; mirrored to `CLAUDE.md`. When you change one router, update the other.
Substantive rules live under `docs/`; this file does not duplicate them.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: a single Python service and CLI named
`tracefold` that turns provider news pushes into audited research signals and
serves them over HTTP / CLI to a React operator console. It has two business
capabilities over one PostgreSQL store — News V3, and the Trading core added in
#104 — and they are siblings, not layers: `tracefold.news` and
`tracefold.trading` never import each other and never read each other's tables,
with `tracefold.app` as the only seam. The former GMGN
social/token/DEX/CEX market lane, Search, Token Case, and the live WebSocket
were removed (#47, #49, #50), and the Macro product line (six current modules,
official-source acquisition, Fed document analyses, and the whole projection/EDF
plane) was removed in #68. See `docs/ARCHITECTURE.md`.

The pipeline is Kappa/CQRS: PostgreSQL material facts (`news_items`) are the
only business truth. The deterministic derived read model (`news_events`) has
exactly one runtime writer and is rebuildable. Current read models use stable
product/window keys, never run/generation/attempt/timestamp/UUID identity. News
V3 is one broker-driven Event pipeline: the authenticated OpenNews WSS pushes
every `strategy.triggered` frame the account owner has enabled provider-side;
a thin Receiver publishes each one to RabbitMQ with publisher confirms and
Tracefold sends no application subscription frame and keeps no local Strategy
allowlist (#126). RabbitMQ (`aio-pika`, quorum queues `news.raw`[SAC] / `news.triage` /
`news.deliver`[SAC], two-level `queue_priority`, one 30 s retry lane, one dead-letter
queue) is the only transport/buffer/retry/concurrency plane for News; queue priority
is scheduling metadata and has no reader/editorial authority; consumers
handle up to `prefetch` messages concurrently, `TransientError` retries are
counted (three, then dead-letter) while `DeferError` (the News DB lane could not
admit the message) requeues uncounted; PostgreSQL holds facts, decisions, and
audit, and every News write is idempotent by key (`news_items(item_id)`,
`news_events(event_id)`, `news_verdicts(event_id, stage, policy_version)`,
`news_deliveries(event_id, kind)`). The Deduper turns Items into Events
(content-block title with pinned source-label normalization that keeps exchange
names and @handles as subjects, exact fingerprint, MinHash/LSH near-duplicates,
strong-fact veto, per-family windows), applies the deterministic Gate —
evidence, not relevance: grounded assets are the provider's B+/A/A+ coin tags
plus literal `$TICKER` cashtags with CL only in energy context, admission is
`candidate` except recovery replays, law-firm templates, and (behind
`news.gate.suppress_low_signal`, default off) low-score ungrounded social posts;
exchange listing/delisting frames take the `listing_deterministic` admission,
which is admitted and goes to Triage like a candidate (#72 — it used to be
stored and then silently dropped), and a stronger later member re-gates a
suppressed Event — and computes a theme-first preliminary storyline key. Triage
is one Program-native `SemanticJudge.judge(TriageContext)` Interface backed by
a code-owned two-Predictor DSPy Module: `EventSemantics.v2` interprets the Event,
model-safe Gate facts and 4 h told ledger and emits nested typed
`TradeRelevanceV1`; a deterministic `SemanticNormalizer` discards a stray
restatement index on `new_fact`/`progression`, canonicalizes relevance sets,
preserves raw values in the call trace, and spends no provider call or fast
retry; then internal `ReaderCard.v2` receives an explicit
`ReaderCardSemanticView` and writes only `headline_zh` and `why_zh`. Neither
Predictor sees `queue_priority`, provider score, Gate macro lexicon or queue lag,
and ReaderCard sees neither delivery intent nor ToldContext. A deterministic
assembler projects the exact `TriageVerdict`; `SemanticJudgment` atomically
carries that verdict, a hashed editorial envelope, trace/usage and runtime
identities, while `ScoredJudgment` is the one typed projection shared by runtime,
baseline, compiler, evaluator and replay. Public `title_zh=""` remains only the
legacy "same as `headline_zh`" sentinel (#101). A normal route is
exactly two serial structured calls. The content-addressed, state-only
`ProgramArtifact v2` separates code-owned QualityKernel/ordered RulePacks from
bounded per-Predictor LearnedStrategy and a typed DemoBank; it also pins
topology, signatures, renderer, four model slots, execution contract and
dependency lock. Rendered prompts are derived bytes, and DSPy cache and hidden
provider retries are disabled. One shared route deadline owns one fast retry;
fallback restarts the whole graph, and the full primary+fallback chain can make
at most six physical requests. This Program is the only semantic filter; the
verdict carries `novelty`
(`new_fact`/`progression`/`restatement` + `restates` index against the ledger);
the final storyline key is computed from the verdict's grounded primaries, then
a theme, then the model's own primaries even when the provider did not tag them,
then a grounded tag the text actually names as its own token, and written back
(#100: the old "any grounded tag" fallback put 16% of a day's asset-keyed cards
in a bucket that was not about them — every OKX listing notice in `asset:OKB`,
Polish jets scrambling in `asset:BTC`); the pure policy-v10 `decide()` owns the
final decision. Queue priority, provider score, macro lexicon and `scope=macro`
cannot select or rescue an action. The ordered action section handles
deterministic listing/telemetry and grounded-watchlist objective guards first,
then accepts `reader_value=escalate|realtime` only when the code-owned
`realtime_eligible` predicate proves magnitude >= 2, a direct/second-order
surface, non-empty channels/markets and a material state/detail change;
`background|none` drops and every inconsistent combination is named. It retains
the grounded restatement, stale-source and similarity order. A Gate-admitted
listing/delisting frame skips the restatement drop and the similarity throttle
only when the matched card names none of its instruments
(`listing_exempt_from_duplicate`, compared as symbol sets, never as headline
text) so one wire template cannot hide several instruments while a genuine
re-send is still withheld. Policy v7
has no hourly, 2 h asset, 4 h theme, or flood quota: historical counts remain
metrics but cannot change a qualified push into a throttle. An ordinary push is
compared with the sent-card ledger using character-bigram Jaccard under
`news.policy.similarity_max`; only a same-fact match is withheld with a `:seen`
key. `similarity_max=0` disables that check without restoring any count cap;
`escalate` and degraded wire-headline fallbacks skip it, and a direction reversal
is never withheld. `decide()` owns the final decision, taken and persisted
inside one transaction under a per-storyline advisory lock (a card landing while
the model was thinking earns one re-ask with the fresh ledger), model failure is
degraded but never silent: only deterministic listing/telemetry or a grounded
watchlist hit fails open; every other failure holds as
`degraded_no_objective_guard`, regardless of score, macro words or queue
priority. A circuit breaker bounds failures, and the verdict carries `audience` plus the
empty compatibility `title_zh` sentinel (no separate translation lane) and a
replayable trace (Program and
runtime-model identity, per-Predictor request/output/usage/cost, every stale
re-ask execution, preliminary and final ledger snapshots, and final storyline
key). There is no Analyst lane: one Event gets
one structured judgment and one card (issue #57); `escalate` is a
high-importance push (⚡ header, AMQP priority), not a second model call.
`queue_priority` only orders broker work and cannot decide the ⚡ header. The
Deliverer performs at most one Feishu attempt
per Event and renders the reader contract card (v10: `headline_zh` header, one
`why_zh` sentence, direction/`新进展`/magnitude/tickers/source/time in plain
words, plus a separate fresh quote line for exactly those assets; price is
display-only and any unavailable/stale quote silently removes that line; no
titles, enums, provider score, or AI label), drops instead of holding when
a crash between send and ack terminalizes as ambiguous instead of resending.
There is no operator pause/mute plane: `news_control_state` never withheld a
card in the whole retained history and was removed rather than left unread.
OpenNews strategy 1019 OI telemetry is the one Event the Program never sees: the
Gate admits it as `telemetry_deterministic` off the frame's own metadata, and
Triage judges it by arithmetic — `tracefold.news.oi_signals` parses the four
numbers, ranks the frame against the symbol's others in a rolling 4 h, and
returns an ordinary `ScoredJudgment` whose editorial origin is
`telemetry_deterministic` and relevance is null, so `decide()`, delivery,
`event_outcome` and the feed all stay on one path. `news_oi_signals` is only the rank ledger; the
decision lives in `news_verdicts` like any other. These Events are exempt from
near-duplicate matching, share the per-instrument duplicate exemption listing
frames got in #72 (one template, different instruments), and are excluded from
ReviewDesk and the model-health denominators (#137). The Janitor
republishes candidates that never left the process, expires bands, and snapshots
broker depths. The instrument universe (`news_market_instruments` +
`news_symbol_aliases`, #75, consolidated in #89) is a rebuildable provider fact
with exactly two jobs: it normalizes the storyline identity so one issuer's
several contracts (SKHY/SKHX/SKHYNIX) share a bucket, and it tells the Gate
whether a headline is about a coin or a stock. A bounded snapshot loop reads the
Binance spot/USD-M and Hyperliquid perp/spot/HIP-3 builder-DEX catalogues (no
credentials; a venue that fails to answer is skipped, never a mass delisting)
and keeps the class the venue itself declares — Binance labels its 169 TradFi
perps `EQUITY`/`CN_EQUITY`/`HK_EQUITY`/`KR_EQUITY`/`COMMODITY`/`PREMARKET`, and
ignoring that field had put 81 of them in the universe as `crypto`; `classify()`
is the fallback for venues (Hyperliquid) that declare nothing. The same loop
reads one _reference_ tier (`us.listed`, #91): the ~13k Nasdaq/NYSE tickers from
the Nasdaq Trader symbol directory, which exist only to tell the Gate that
`UWMC` or `TLX` is a stock — 133 Events in a week were grounded on an equity no
crypto venue lists and read as crypto. It never overrides a traded symbol (352
crypto base symbols are also US tickers: `ATOM` is Atomera, `BCH` is Banco de
Chile), it is excluded from `asset_refs`, the console's `符号落表` funnel and
the `在交易合约` figure, and index membership was measured and rejected as a
source (S&P 500 covers 4.6% of the residue — the large caps already have Binance
TradFi perps). Code-owned seed aliases are reconciled into the table on every
snapshot — the code wins, and a seed pointing at a symbol no venue lists is
reported (`status.instruments.dangling_aliases`) instead of resolving to nothing
forever. It is deliberately neither a filter nor a source of listing events:
#75's existence whitelist was retired after a dry-run showed it only ever
removed real equities with no crypto perp, and listing/delisting facts arrive as
provider frames (#72) that the snapshot diff could only have duplicated for the
two venues we poll. The learning plane is immutable `EventEvidenceSnapshot`
plus append-only multi-dimensional reviews/external misses and one
`CandidateEvaluator`. The deployment-time `program_v6` epoch for
`tracefold.news.semantic_program.factory_v4` / `news_semantic_program_v4` on
the artifact-v2 envelope makes every
earlier Prompt/Program cohort audit-only and reaccrues release evidence from
zero. It freezes accepted evidence, runs stable
and exactly one declared `program` or
`policy` variable sequentially, then requires sealed future holdout, blind
pairwise, shadow and deterministic one-arm canary evidence before promotion.
Every new verdict binds the exact runtime manifest. Canary selector v2 includes
queue-high Events, excludes recovery/listing/telemetry lanes, and fails closed
at startup, resume and assignment if selector, eligibility-profile or rolling-
profile identity drifts. A
cold, manually invoked DSPy GEPA workflow receives a trusted sealed development
corpus without DB/holdout/application credentials, can emit only a bounded
two-instruction `ProgramPatchV2`, and runs under explicit metric/model/
cost/resource/seed budgets. Compiler protocol/receipt v3 seals distinct task,
reflection and `metric_judge` role configurations; reflection owns its 32k-token
budget, while the judge has a separate identity, tariff, calls/cost/failure
receipt and returns explicit unavailable on failure. A trusted applier constructs the unaccepted
Artifact; the optimizer can never accept, deploy, promote, or edit the trusted
root. `news learning baseline` (#143) is the cold, read-only `dspy.Evaluate`
step that has to come first: same graph, same `decide()`, and literally the same
`accepted_review_metric` object the optimizer maximizes, with no dataset,
sandbox, tariff, container or write of any kind, and one `serve` connection that
closes before the first model call. #150 split its one ambiguous `live` mode
into three that answer three questions: `recorded` scores the persisted verdict
against the action that shipped and spends no provider call, `compile_live` runs
exactly the graph GEPA optimizes on one task endpoint with no fallback, retry,
deadline or breaker, and `runtime_live` runs the configured four-slot production
Program route sequentially in `(opened_at_ms, case_id)` order so circuit state
is a property of the run — while naming the consumer transaction, advisory lock,
stale re-ask, degraded wire card, broker and delivery it still excludes. The v2
report has no single ambiguous scalar: a provider failure is an outcome, so
quality-given-an-answer and the failure-as-zero lower bound are published side
by side (29 unanswered cases had turned a 0.482 lower bound into a printed
0.587), `review_label_distribution` is corpus metadata while
`prediction_dimensions` is what the candidate did, a hard-gated case keeps its
action and its per-dimension outcomes so a zero enters every denominator rather
than leaving it, and `timeliness` is delivery-owned: it leaves the EventSemantics
score and stays visible under the label distribution's `not_scored` group. Policy is frozen into
each scored example (`policy_values` + `policy_sha256`, verified) instead of
imported from `DEFAULT_POLICY`, and a missing or tampered policy raises rather
than scoring. The recorded calibration is pinned to a checked-in redacted corpus rather than
to the live database, because a number that moves when the corpus grows cannot
prove that metric wiring is unchanged; the expected values live only in
`tests/news/test_news_baseline_calibration.py`, so there is one place to read
and one place to update.
Metric v4 (`tracefold.news.production_action_trade_relevance_v4`) weights the
exact final production action 45%, exact TradeRelevance gold 35%,
semantics/novelty 10% and ReaderCard 10%, using the same version-bound
`DecisionResult` projection everywhere. `dspy.GEPA` only rewrites instructions and never writes demos, so
DemoBank stays empty under this optimizer by construction; the reflection
endpoint is configured separately from the task endpoint with its own 32k-token,
temperature-1.0 budget, and a code-owned proposer shows the reflection model the
full rendered RulePacks it is amending. Reviews are accepted under
`news_review_v4`; every failed scored dimension needs exact expected gold, and
only accepted v4 reviews from the v6 epoch enter metric v4, GEPA or release
evidence. Older reviews remain audit-only. `news.retention` keeps raw
Items 30 days and judged/reviewed ones 365. Reader HTTP/OpenAPI/React expose no
priority field, filter, sort or loudness badge; `queue_priority` remains visible
only to transport and explicit operator audit surfaces. `/api/news/status.pipeline` reports
where the last 24 h went
(`suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`,
`pushed_by_rule`, `reviewed_should_push_24h`, `reviewed_external_miss_24h`,
`candidate_share_24h`), `status.health`/`funnel_24h`/`reasons_24h` are the
thresholded, Chinese-labelled view of the same facts, and every Event carries
one server-owned `outcome` (`tracefold.news.outcome`, ten stable kinds with
`text_zh`/`reason_zh`) shared by the feed, the detail timeline, and `news why`
(issue #60). The Price Review plane (#88) is the third consumer of the
instrument universe and the only place a price exists: two cold Workers loops,
sharing one exact-symbol-first resolution strategy (an alias is a fallback,
never a substitute — `SKHX` prices SKHX even though the throttle buckets it
under `SKHY` — and `us.listed` is never a price candidate), write two derived
read models with deliberately different lifecycles. `news_quote_snapshots` is
latest-only current display state: every 20 s (a 5 s cadence bought no freshness
the HTTP-polling browser could show, and cost 9.8 GB/day because Binance's USD-M
ticker has no `symbols=` filter) the planner deduplicates recent live Events
plus the watchlist into unique Price Instruments (bounded at 256 across at most
12 source groups), issues at most one batch REST request per source (Hyperliquid
`metaAndAssetCtxs` / `spotMetaAndAssetCtxs`, one per active HIP-3 dex; Binance
`ticker/price`, except on the one turn in fifteen where that source instead
reads `ticker/24hr` — 270 kB against 45.5 kB for the same USD-M market, so #109
pays for it every 300 s rather than every 20 s, and the day read _replaces_ that
turn's price read so a turn is still exactly one request per source; no
credentials, concurrency 4, a 10 s turn deadline, turns never overlapping), and
replaces one row per source — so a hundred Events naming BTC are one target and
one provider result, and a venue that fails leaves its previous row to age into
`stale` rather than blanking to zero. What the wide read leaves behind is the
rolling window's `openPrice`, not a percentage: the day change is recomputed
from every turn's own price against that cached reference, so the number can
never disagree with the price rendered beside it, and what ages between day
reads is a 24 h window open that moves 0.023% per turn. Nothing is cached for a
read that failed or was cancelled, so a failed day read leaves the source due,
writes nothing, and lets its previous row age — the same stale-not-blank rule as
any other venue failure. A symbol that joins the working set is read wide
immediately rather than waiting out the cadence, because the newest Event is the
card the operator is looking at. `news_event_reactions` is the deterministic
answer to "what did the market do after this": a 60 s loop scans at most 100 due
Event-assets over _every_ live grounded Event (held ones included — that is what
the miss review needs), merges their candle ranges into at most 32 requests,
takes `p0` as the last closed 5 m candle at or before `opened_at_ms` (the
provider publication time, never delivery) and `p1`/`p4` the same at +1H/+4H,
and stores `(pH/p0)-1` as integer basis points keyed by
`(event_id, symbol, metric_version)`; `reaction_v1` freezes interval, alignment,
gap tolerance, source selection, aggregation and the hit definition, a gap is
`no_candle_within_gap` and never forward-filled, and a transient provider
failure writes nothing so the work stays due. `/api/news/quotes` (≤100 symbols,
`fresh|stale|unavailable|unlisted`) is deliberately not a feed field — a price
that changed must not invalidate the Feed ETag every 3 s — while
`/api/news/review` serves ReviewDesk; its market view defaults to one exact
Program/policy/runtime-model cohort, uses mature denominators, clusters held
Events at fact grain, and cannot promote a candidate. Movement proves no
causality and is
never reward or `should_push` truth. Price never reaches the
Gate, Triage, `decide()`, a duplicate key or a ranking signal; card v10 may
render a fresh quote as display-only reader context. The loops
admit their DB work through a one-slot cold lane rather than the four News hot
slots, and there is still no market-mark lane, tick history, OI, order book, or
market socket — #109 measured the socket question rather than arguing it:
Binance's futures WSS delivered zero frames in 22 s from the deployment host
across all three documented URL forms while its REST worked, and the spot socket
that does deliver costs 3.5 GB/day for 218 symbols against 0.26 GB/day for the
whole USD-M REST plane after #109 (a subscription is only cheaper than polling
when you consume faster than the venue pushes, and the console polls), while a
socket cannot reach an HTTP-polling browser any faster anyway, so
`docs/ARCHITECTURE.md` records a four-part promotion criterion instead of a
preference. Recovery of closed incidents uses the official Strategy hits
endpoints and recovered Items never deliver. WorldMonitor RSS, Story, Brief, and
pinned scoring are retired; deepagents is not a dependency. News consumers
recover by re-consuming durable broker queues plus database idempotency; there
is no database wake plane, no projection/EDF frontier, and no durable queue
terminal-evidence lane. Provider raw frames are inputs, not facts.

`tracefold.trading` (#104) is the capital lane and is disabled by default. It
consumes two public News projections — deterministic OI telemetry verdicts and
model Triage verdicts — through the composition root, canonicalises the symbol,
applies an operator-owned deny-list seeded with `BTC`/`ETH`/`CL` (one row blocks
every provider spelling, and a read failure blocks everything), resolves exactly
one native perp on `binance.perp` or `hl.perp`, and computes a deterministic
OI/price quadrant. OI direction is never a side on its own; the pre-move filter
is a measured **band** (default 100–600 bps over 1 h), because
`oi-agent-design-2026-08-22.md` §1.6 found an inverted U over 630 frames with
every losing bucket above the band. An `oi_only` case is arithmetic and calls no
model — that is the high-frequency execution-kernel trial lane and it never
reaches live; a News-bearing case spends exactly one `dspy.Predict` call with no
tools, no agent framework anywhere in the tree, and every failure resolving to
`no_trade`; a pure policy then maps context to `no_trade | long | short` and
names its rule. Sizing is fixed-notional at 1x with a fixed stop and no
take-profit, so the worst case is `fixed_notional x fixed_stop_bps x
max_orders_per_day`. Because OpenTrade publishes no client idempotency key, the
ledger commits `SUBMITTING` before the network call, `provider_attempt_count` is
CHECKed at one, and a timeout, malformed answer or restart terminalises as
`AMBIGUOUS` whose only legal successor is a read — never a resend, never a
resend routed at the other venue. A partial unique index keeps one active order
per venue-independent `underlying_key` in every state that can carry exposure,
`APPROVED`/`RECONCILING`/`MANUAL_REVIEW_REQUIRED`/`UNPROTECTED` included. Two
deterministic exits: the venue-side stop and `max_holding_seconds`. Five
`trading_*` tables, two cold runners in the existing Workers root sharing the
price plane's one-slot DB lane, no new queue, no new deployable, no UI, and no
profitability claim from a paper trial.

Which Strategies feed News is decided in the OpenNews account and nowhere else
(#126). Tracefold sends no subscription frame, so the socket delivers what the
account has enabled; there is no `news.opennews_strategy_ids`, the Receiver
filters nothing, and adding or removing a source is a dashboard switch that
takes effect without a config edit or a restart. The old local allowlist was a
second switch for the same decision and had drifted: it listed `1672`
行情异动监控, which had produced nothing in seven days, while silently dropping
every `1019` OI Event Monitor frame the provider was pushing. `/api/news/status`
says nothing about Strategies at all — not the IDs, not a count: Tracefold
neither chooses nor filters them, so a figure there would only restate the
provider's dashboard. Recovery is the one place that still needs the list, read
live from the account because the provider's hits endpoint is per-strategy. One
ID, `1353`, is read by the Gate off each Event's own provider metadata to mark a
listing/delisting frame, which is provenance rather than configuration.

## Agent skills

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD
tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before
domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for
application/provider/credential/storage settings. Worker topology and
safety/resource budgets are code-owned. Do not assume repository fixtures,
example YAML, or `.env` files are the active runtime config. Before debugging
provider data or News events against real data, run `uv run tracefold config`
and confirm the reported `config_path` points at `~/.tracefold/config.yaml`.
Never print or copy secret values; report only redacted booleans, paths, and
diagnostic command results.

## Frontend guardrails

Frontend CSS is harness-constrained, not convention-only. Before changing
`web/src` UI code, read `docs/FRONTEND.md`. Do not recreate retired CSS buckets
such as `cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, or
`signalLab.css`; owner CSS must live beside the component or route that imports
it. Feature CSS must use the owning feature namespace and must not restyle
shared UI internals or Obsidian `.ods-*` selectors. `npm run lint` runs ESLint
plus the frontend architecture harness; do not bypass it after CSS, responsive,
route shell, or shared UI changes.

## Where to read what

| Need                                      | File                                                                                     |
| ----------------------------------------- | ---------------------------------------------------------------------------------------- |
| Install, run, docker                      | `docs/SETUP.md`                                                                          |
| Layer boundaries & data flow              | `docs/ARCHITECTURE.md`                                                                   |
| Frontend architecture                     | `docs/FRONTEND.md`                                                                       |
| Public surfaces (config, HTTP, CLI)       | `docs/CONTRACTS.md`                                                                      |
| Development, issue specs, design, testing | `docs/DEVELOPMENT.md`                                                                    |
| Secrets, config, authn changes            | `docs/SECURITY.md`                                                                       |
| Operations, workers, PostgreSQL diagnosis | `docs/OPERATIONS.md`                                                                     |
| Business package boundaries               | `docs/ARCHITECTURE.md`; the public Python interfaces are the `tracefold.news` and `tracefold.trading` package roots |
| Durable specs and acceptance              | GitHub Issues; repository conventions are in `docs/agents/issue-tracker.md`              |
| Auto-generated artefacts                  | `docs/generated/`                                                                        |

CLI surface: `uv run tracefold --help` is the source of truth (snapshot at
`docs/generated/cli-help.md`).

<!-- END SHARED AGENT ROUTER -->

## Codex-only protocol: worktrees, GitHub, planning, and skills

### Worktree by default (do not switch the primary checkout)

The primary checkout (`~/Documents/Code/tracefold`) is the deployment checkout:
it stays on `main`, stays clean, and is the only place `make up` / `make status`
/ `make logs` run. Never switch branches there, edit files there for a task, or
move another task's uncommitted changes. Every code or docs change — including
one-line fixes — starts in its own worktree so tasks and Codex chats can proceed
in parallel.

At the start of every change task, before editing:

1. Run `git worktree list --porcelain`, `git status --short --branch`, and
   `git rev-parse --show-toplevel`.
2. If the chat is already in a Codex-managed, permanent, or sibling worktree
   dedicated to this task, stay there; do not create a nested worktree.
3. If the chat is in the primary checkout, do not edit there. Create an unused
   sibling worktree from `origin/main`, then direct every subsequent command,
   patch, and test at that worktree:

```bash
git fetch origin main
git worktree add -b codex/<issue-or-slug> ../tracefold-<slug> origin/main
cd ../tracefold-<slug> && uv sync && (cd web && npm ci)   # per-worktree venv/node_modules
```

- In the Codex desktop app, starting the chat with **Worktree** selected is the
  preferred equivalent. A Codex-managed worktree starts at detached `HEAD`; before
  editing, create a `codex/<issue>-<slug>` branch there from `origin/main` (or use
  **Create branch here**). If this chat already has an assigned task branch, keep
  it instead of creating another one.
- Branch from `origin/main`, not local `main`. Name branches
  `codex/<issue>-<slug>` (or `codex/<slug>` when no issue exists). One task, one
  worktree, one branch, one PR.
- An existing worktree belongs to its current task. Never reuse, reset, delete,
  prune, or clean it unless it is the current task's worktree and cleanup is part
  of the authorized finish flow.
- Run tests inside the task worktree (`make test`, plus focused lanes from
  `docs/DEVELOPMENT.md`). The compose stack (`make up`, ports 8765/8766/5672, and
  the `tracefold-*` volumes) is shared and owned by the primary checkout; do not
  run `make up` or `make down` from a worktree.
- Commit and push only from the task branch. Never commit or push on `main`
  directly.
- Finish through PR review and merge. After merge, update and deploy only from
  the primary checkout (`git pull --ff-only origin main`, then `make up`). Remove
  the task worktree and delete its branch only after the merge is confirmed and
  the cleanup is authorized; use `git worktree prune` only for entries verified
  stale.

### GitHub issue, planning, and review

- Non-trivial changes (new table/worker/queue/prompt/policy, hard cuts) start
  with the GitHub issue (`docs/agents/issue-tracker.md`) and a written Codex plan.
  Keep scope and durable decisions in the issue rather than only in chat.
- Use the `code-review` skill on the task branch before opening the PR. Use the
  security-review capability as well when config, auth, credentials, permissions,
  or delivery code changes and that capability is available.
- Live-data debugging follows "Runtime config for real data" above: use read-only
  SQL as the `tracefold_serve` role, run `uv run tracefold config`, and never print
  secrets.
