# Frontend

> **Scope.** Owns the `web/` architecture, layer responsibilities, component conventions, and the UI verification gate. Backend layer boundaries live in `ARCHITECTURE.md`; public HTTP contracts live in `CONTRACTS.md`; install and run commands live in `SETUP.md`.

The React operator console is a News workbench plus one actionable Alpha/Execution desk. It reads exactly `/api/bootstrap`, `/api/status`, `/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/market`, `/api/news/market/{item_id}`, `/api/news/status`, `/api/news/quotes`, `/api/news/symbols/{base}`, `/api/trading/status`, `/api/trading/cases`, and `/api/trading/executions` over HTTP — twelve reads and one write. `/api/trading/gate` and `/api/trading/gate/{event_id}` stay on the server and have no browser reader: the OI frame table joined each admission row to its Event on the same line, and #553 PR-1 removed that join with the Events themselves. The two market reads arrived with #553 PR-1: OI frames, liquidations, smart-money prints and market sources we have no parser for are stored facts rather than Events, so the Event feed cannot serve them and `/api/news/status` no longer counts them. `GET /api/trading/signals` and the two `GET /api/trading/execution/*` projections were deleted in #537 PR-5: no browser surface called any of the three, they were three more public shapes over the ledgers `/api/trading/executions` already reads folded, and `tracefold trading signals | observations | commands` reads the same repository directly. Every operation is a read except the exact authenticated `POST /api/trading/execution/commands`, which can append only pause, resume, or account-flatten intents in the existing closed grammar. It cannot submit an order or accept quantity, notional, leverage, venue, or direction. There is no WebSocket client, no separate Search route, no Token Case, no token identity or DEX/CEX market surface, no provider image lane, and no Macro workbench.

## Source Layer Map (`web/src/`)

| Directory                | Responsibility                                                                                                                                                                                                    |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/`                   | Application composition: providers, router wiring, top-level error boundary, and route fallback. It may compose feature route elements, but it must not own feature data queries or business rendering.           |
| `routes/`                | Route entries and URL-state orchestration. Route modules parse/serialize shareable state and choose the owning feature view.                                                                                      |
| `features/<name>/api/`   | Feature-owned endpoint adapters, query keys, and reusable server-state hooks. Feature public hooks/controllers may own narrow server reads when they are the feature boundary consumed by routes or UI.           |
| `features/<name>/model/` | Pure feature helpers, view models, and constants. Framework-free where practical.                                                                                                                                 |
| `features/<name>/state/` | Local client state that is not shareable URL state and not server cache state. Keep it narrow and feature-owned.                                                                                                  |
| `features/<name>/ui/`    | Feature screens and components. UI reads data from props or feature hooks exposed through the feature public index, not from another feature's deep files.                                                        |
| `shared/hooks/`          | Framework hooks that belong to no feature, such as `useMediaQuery` (which frame to *mount*).                                                                                                                       |
| `shared/query/`          | Cross-feature React Query key helpers. Feature hooks own their queries; there is no cross-feature cache patching.                                                                                                 |
| `shared/routing/`        | Reusable route parsing, path building, and URL search-param helpers.                                                                                                                                              |
| `shared/ui/`             | Reusable presentational primitives (`Card`, `Metric`, `Bar`, `FactGrid`, `KeyValue`, `ActionButton`, `IconButton`, `PageState`, `Toast`, `Drawer`, `RouteBackLink`). No server fetching. |
| `lib/api/`               | Typed HTTP client facade and auth-token plumbing. No feature query hooks.                                                                                                                                         |
| `lib/env/`               | Runtime environment parsing.                                                                                                                                                                                      |
| `lib/types/`             | Generated OpenAPI types (`openapi.ts`, regenerated by `npm run generate:types`) plus the frontend-owned `ApiResponse` envelope.                                                                                   |
| `styles/`                | Application-global styles and their local imports. Feature/page selectors belong beside their owning component or feature as side-effect CSS, or as real CSS Modules with local class bindings. |

Do not add new code under old `api/`, `store/`, or `components/` roots. Public feature imports should come from `@features/<name>`; sanctioned route-shell entrypoints may use `@features/<name>/shell`. Deep imports across feature internals are blocked by lint and grep gates; the relative-import boundary gate derives feature roots from `web/src/features`.

`features/news` follows that map: `api/newsQueries.ts` (query hooks and contract
types), `model/` (`newsLabels.ts`, `newsTime.ts`, `newsPrice.ts`, `feedFilters.ts` — the
URL-owned feed state — and `marketFacts.ts` — the market page's kind vocabulary and its
URL-owned `?kind=`), `state/` (`useAnchoredEventFeed`), and
`ui/` split into `chrome/` (the frame, tone grammar, outcome badge, direction chip,
asset chips, quote values, health pill — anything more than one surface renders),
`feed/`, `detail/`, `status/`, `market/` and `symbol/`. `features/news/shell.ts` is the shell
entrypoint and exports hooks, pure helpers and types only, so importing it does not pull
the route components into the eager shell chunk.

## Test Map (`web/tests/`)

`web/src/` contains production frontend code only. Frontend Vitest, React Testing Library, MSW, fixtures, architecture gates, and Playwright specs live under `web/tests/`. Repository-root `tests/` remains the Python/FastAPI pytest tree.

| Directory           | Responsibility                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------ |
| `unit/`             | Pure model, state, mapper, and library tests that mirror production source paths.                      |
| `component/`        | Focused React component, hook, and feature API hook tests.                                             |
| `routes/`           | App and route integration tests that render `App` or route shells.                                     |
| `architecture/`     | Static source gates for import boundaries, CSS ownership, test placement, and dead compatibility code. |
| `fixtures/`         | Shared frontend test fixtures.                                                                         |
| `msw/`              | MSW server, handlers, and named API scenarios.                                                         |
| `render/`           | React Testing Library render wrappers and route render harnesses.                                      |
| `e2e/golden-paths/` | Playwright browser golden paths.                                                                       |
| `e2e/full-stack/`   | Required Chromium smoke against real FastAPI static/bootstrap/API reads.                               |

## Conventions

- **Design contract and page archetypes.** Tracefold has one light, restrained
  operator-workbench language: 冷石板 / B SLATE (#74, redesigned to the v6 spec). The
  console is read in Chinese, and Han glyphs bloom under light-on-dark at these sizes,
  so the ground is a cool grey `#eaedf1` and the ink is near-black. A panel is white,
  radius 10, separated from the canvas by a 1px *inset* ring (`--ring-panel`) — never by
  lightening and never by a drop shadow. Real elevation is spent on exactly four things:
  the segmented control's selected pill, the drawer, the pipeline-health popover, and the
  time menu. `styles/tokens.css` is the only semantic colour, type, radius, depth, focus
  and shell token contract; production code must not add a parallel theme or
  compatibility alias, and it must not define a colour, a type step or a radius outside
  it. The type scale is seven steps (`--type-page-title` … `--type-eyebrow`) and there is
  no eighth: Chinese body text is set in the system sans, and every figure, ticker, key
  and duration is monospace with tabular numerals so a number that changes on a poll
  never re-flows the ones beside it.

  Stable routes declare one of two information archetypes with `data-page-archetype`:
  `scan` for the News Event feed, 市场事实, the token page and News status, which sit in a
  1340px measure;
  `case` for News Event detail, one document centred at 1000px whose hero leads with the
  model's market direction. The archetype governs measure, hierarchy and density, never
  data ownership or business inference.
- **Data ownership.** Feature-owned API hooks, page hooks, and controller hooks own server reads/writes. Route modules and presentational UI components consume those feature hooks and must not call `useQuery`, `useMutation`, `useInfiniteQuery`, `getApi`, `postApi`, or `queryClient.set*` directly. The frontend architecture lane enforces this boundary for `web/src/routes` and `web/src/features/*/ui`.
- **URL state.** Shareable route options (News feed `q`/filters/sort) live in
  the URL and their owning route-state helpers. No route
  carries hidden same-session scroll or selection state that would not survive
  a hard reload.
- **Transport.** The browser talks HTTP only. `useAppSession` reads
  `/api/bootstrap` (`{ws_token}`) once and installs the bearer token on
  `lib/api/client`; feature hooks poll `/api/news/*` on
  code-owned intervals with ETag revalidation. The Trading desk's three Command
  buttons write with that same bearer: the separate 0600 operator write token
  and the Live `CONFIRM` re-entry went with #520 PR-B, and one credential for
  the read and the one bounded append is the whole authority model. There is no `/ws` client, no
  socket provider, no live-market cache patching, and no subscription registry;
  the shell status pill derives only from `/api/status.runtime`.
- **Topbar search.** The single topbar search box is News-only: its label is
  `news search`. Its placeholder is `标的 / 事件关键词` and the `/` key is an inert visual keycap, on
  every route (#256): the artifact draws one topbar, and a box that renamed itself between surfaces taught
  the reader that the two searched different things when they never did. Submit
  always navigates to a fresh `/news?q=<query>&outcome=all&hours=168` scope from
  every route; it never carries a previous cursor or hidden feed filter, and an
  empty submit produces the same default scope without `q`. The `/` key has no
  special behaviour. The server reports whether the query was classified as
  exact asset identity or Event text; the feed renders that visible scope and a
  mode-specific zero-result explanation. The feed toolbar may change its own
  visible filters while preserving `q`. There is no global token/handle/CA
  search and no second search entry inside the News feed.
- **Topbar context figures.** The right side identifies the active workbench with facts already present in
  the shared News and Trading status queries. The Event feed shows `PUSHED 24H / E2E P50`; other News
  reading surfaces retain `PUSHED 24H / E2E P95`, pipeline status shows `EVENTS 24H / QUEUE P95`, and Trading
  shows `ALPHA / EXECUTION / SIGNALS 24H`. 市场事实 shows none (#553 PR-1): its figure read
  `pipeline.telemetry_parsed_24h`, which is not a status field any more, and market intake is a per-kind
  figure the page itself leads with — a frame figure could only be a second poll of the page's own endpoint. No route starts an extra request for chrome and the browser does
  not derive a rate, score, or readiness state. These figures leave the phone topbar; Trading repeats its
  three safety facts in the page header at that width.
- **News routes.** `/news` is a decision-first scan surface over the flat
  Event feed from `/api/news/feed`; the browser never clusters, scores,
  triages, throttles, or reorders. The public News navigation contains
  exactly `事件流`, `市场事实` and `交易`.

  `Alpha 判定` at `/news/alpha` was a fourth destination until #460. It read one
  endpoint — `/api/trading/cases` — and so does `/trading`, so the list of Cases
  existed twice. The thing it showed that `/trading` did not was one Case's
  frozen evidence, and that moved into the Trading Case card rather than being
  deleted with the page: selecting a row there opens the Case's terminal answer,
  its identity and timestamps, and the frozen check table (check, operator,
  threshold, measured, pass/fail) with its frozen `policy_config` beneath it. A
  Case written before `policy_checks` existed says so rather than showing an
  empty table, and every threshold on screen is the Case's own, never today's
  configuration. `/trading?case=<id>` opens one directly, and the desk's own Case
  rows link that way. A `case` the rolling window has
  already dropped says so and names the window rather than rendering nothing.


  `/news/market` is `市场事实` (#553 PR-1, replacing `/news/oi` and the OI
  audit before it). OI frames, liquidations, smart-money prints and market
  sources we have no parser for are not Events: they are observations, stored as
  facts, and this is the only browser surface that reads them. Nothing on the
  page is judged, scored, admitted or pushed by the console — the Event feed's
  vocabulary does not apply here, and neither does the Signal lane's.

  It reads `/api/news/market` for the page and `/api/news/market/{item_id}` when
  a reader expands one group. The list request carries `kind`, `limit` and a
  `cursor`; the window is the endpoint's own default of the last 72 h, absolute
  rather than a rolling offset, and neither `from_ms` nor `to_ms` is browser
  state yet. Consecutive observations of one `group_key` arrive collapsed to
  their newest member with the run's `observation_count` and its first/last
  clock, so a liquidation cascade is one row that says how deep it went rather
  than four hundred.

  **Three independent reads, three independent failures.** The list is the page:
  `/api/news/market` answering is the only precondition for a row. This is the
  defect the cut exists to remove — `/news/oi` wrapped its whole body in a
  `PageState.Stale` gated on `/api/news/status`, so a 5xx on a pipeline
  dashboard endpoint blanked the telemetry data beside it. Status is now one
  supporting read behind one strip: the ingest wire, which is the single
  question the stored rows cannot answer, since an empty window is either a
  quiet market or a dropped connection. Its failure names itself and reaches
  nothing else. The third read is one expanded group's Item, and it fails inside
  that group and nowhere else. And the page reads no Trading endpoint at all: a
  market observation carries no `event_id`, so there is no key for an admission
  verdict to join on and nothing for `/api/trading/gate` to answer. Nothing in
  the browser reads that endpoint any more.

  **Parse and push are two answers, never one column.** `parse_status` /
  `parse_error` say what the parser could read out of the provider's record;
  `notification_status` / `notification_reason` say what the notification owner
  did with it. A record that parsed cleanly and was never pushed, and one that
  never parsed at all, are different operational states, so they never share a
  cell, a colour or a word. `notification_status`, `notification_reason` and
  `parse_error` are open server strings and are printed verbatim — the operator
  greps them, and a Chinese gloss invented in the browser would either rename
  one or silently swallow a value this build has not seen. There is no
  page-level "is push wired" banner (#553 PR-2): one group can be `sent` while
  another is `merging` in the same moment, so a single banner would be a second,
  weaker answer to a question every row already answers. The expanded detail adds
  the card's own numbers — its trigger reason, how many observations it spoke
  for, how many attempts it took, and which provider answered — because "sent"
  without them cannot be checked against the timeline beside it.

  Five kinds share that strip. Four of them are provider frames; `wallet` is not
  (#572 PR-2) — the chain tape derives it from the fills of the followed wallets,
  so it is always `parsed`, always `robinhood_chain`, and its `wallet_*` fields
  are absent on every other kind. The console has no page of its own for it yet;
  it appears in the market list and the market detail like any other kind.

  `raw` is a shape, not a failure. An `unknown_market` source has no parser at
  all, and its record is retained with its provider line and its stated reason —
  which is why the per-kind strip puts `raw` beside `parsed` rather than under
  it. That strip is `sources[]`, counted off the stored facts in the same
  response as the rows, so the summary and the rows cannot disagree about what
  the window holds. Its second line is the receipt half — `merged`, `sent`,
  `failed`, `unknown` — drawn quieter than the intake because it answers a
  different question: what a reader was actually told about what arrived. Every kind keeps a tile whether or not it sent anything:
  「这个来源 72 小时没发过东西」 is the answer a reader came for.

  The four kind chips write `?kind=` as the server's own comma-separated subset
  and each selection is a real request, because `sources[]` describes the whole window
  and a browser-side split would leave the strip disagreeing with the rows under
  it. A subset that narrows nothing — empty, or all four — is the absence of the
  filter rather than a spelled-out list, so it shares one query key and one
  `filters.kind` with the unfiltered page. Expanding a group is one request for
  the group's whole retained timeline, never one per member, and it is not
  polled: a stored provider payload cannot change.

  The row shows the provider's own line, the parser's subject when it read one,
  and the stored columns in the expansion. It re-parses nothing: deriving a
  number from `title` in the browser would be the ingest parser running a second
  time, drifting from the stored fact the moment either side changed. It draws
  no open-interest curve — the provider emits a record only when its own trigger
  fires, so a line between them is invented — and it edits no threshold.

  `/news/symbols/:base` is 代币页 (#207 PR-W1): what one `base_symbol` is, and
  everything that happened to it. It reads three endpoints, each on its own key
  and its own rhythm — `/api/news/symbols/{base}` for identity (`contracts`,
  `tradeable`, `normalization`), `/api/news/feed?symbol=&hours=24` for the
  Events, and `/api/news/quotes` for the current mark — and closes every panel
  with the same mono source line 市场事实 uses. Identity does not poll:
  the universe snapshot lands on a schedule measured in hours, and the two
  things that do move each arrive on their own key.

  The identity card keeps three answers separate. `known` is whether any venue
  we poll lists the name; `tradeable` is the #91 distinction — a `us.listed`
  contract proves a ticker exists, not that the lane can act on it, so a
  reference-only row is rendered *and* labelled rather than filtered away. The
  quote is a third poll and is never a zero. A base the universe has never seen
  is `known: false` with an explanation, not a 404: every asset chip on the
  console links here, including the struck-through ones, so 404 would be the
  ordinary outcome of following one.

  The events table mixes both Event kinds on one clock — the reason a
  per-token page exists — and its channel tabs (`全部 / 已推送 / 新闻 /
  上币/下币`) filter the loaded window in the browser, unlike 市场事实's
  server-side kind chips. That is deliberate and stated in the source
  line: `/api/news/feed` has no `event_kind` parameter, so a server count would
  describe a window the table is not showing; the tab count and the rows under
  it come from the same loaded set. `OI 帧 / 强平 / 未支持市场` left this strip
  with the Events behind them (#553 PR-1): those records are market observations
  and are read on 市场事实, so a tab here could only ever count zero. The lane is the server's `event_kind`, never
  a guess from `admission`, title, or verdict. The rendered token page has no
  watchlist control, price chart, or open-interest curve.

  The token page reads no Trading endpoint at all (#537 PR-5). It carried one
  `Alpha 复盘 · Case → Signal` section from #433-C, reading
  `/api/trading/cases?underlying=` and `/api/trading/signals?market=…` and
  joining them by `case_id`: a second Case list, on a News surface, over the two
  ledgers the desk renders folded into one row per entry with its whole venue
  outcome. The desk is where a Case and what the Runtime did with it are read
  together; this page answers what happened to one name.

  **Historical pre-433-C UI note (retired).** The previous artifact's order was
  identity band, then the capital
  lane's two sections, then the event list. 交易视角 · 最近一帧怎么读 reads the
  newest case the lane opened for this token and shows three things in the order
  the lane asks them — which quadrant it assigned, where the move that had
  already happened sits against the band the strategy would still enter inside,
  and how the frame's measurements compare with the floors. 交易复盘 lists every
  case in the window, and expanding a row shows the named rule beside the frozen
  `strategy_config` it was decided against. Both read one
  `/api/trading/intents?underlying=` batch, so they share a poll, and the frame is
  attached by the `event_id` the ledger itself published — never by symbol and
  time, the join 市场事实 still refuses to guess at.

  Each of the band's three figures is printed once, where the artifact puts
  them: 24H 事件 and 已推送 have no second copy in the page header, and the OI
  window figure has none in the rank card below, which is left carrying the
  consequence — a full window withholds this name's next qualifying frame —
  because that is what no tile can hold. The 结果 column keeps the ledger's bps
  rather than the artifact's percent, so one field reads the same on this page
  and on the Trading workbench.

  Every threshold in both sections is the case's own frozen one. The artifact
  hard-codes a 1–6% band and calls the window 「帧前 1H」; those were the numbers
  of the strategy running when it was drawn, and today's are 0–10% over five
  minutes, so the band's edges, its caption and the floor rows all come out of
  `strategy_config`. A case that froze none says so rather than borrowing
  today's configuration.

  The floor table is keyed by `strategy_id`, and each row carries the operator
  its own strategy refuses on. `/api/trading/cases?underlying=` filters on the
  name alone, so this token's newest case can belong to any identity the ledger
  holds, and identities freeze disjoint `strategy_config` key sets — one
  identity's rows over another's case label every row 未冻结 and explain nothing.
  Inclusivity is per key as well: the OI × smart-money template refuses
  `whale_oi_ratio_bps <= floor` and `whale_long_profit_bps <= floor`, its own
  docstring calls that non-negotiable, and its shipped profit floor is 0 — so a
  table reading `>=` everywhere stamped 过地板 on exactly the frames the ledger
  refused. A row prints `>` or `≥` as the strategy wrote it.

  A floor row has four answers, not three: 过地板 and 低于地板 are comparisons,
  未冻结 is the case having frozen no such floor, and 未测量 is a frozen floor
  over a frame carrying nothing to compare it with — the common case, since most
  cases publish no joinable `event_id` at all. Collapsing the last two printed
  未冻结 in the same row as the `≥ 95.00%` the case had frozen. The sentence
  under the table is counted off those rows for the same reason: 「一条都没过」 is
  itself a measurement, and it was being asserted over rows that had never been
  read. That sentence names the reader gate only when the frame's own `delivery`
  says it was sent — the capital lane deliberately consumes frames the reader
  withheld, so 「这一帧推送了」 over one of those states the opposite of the
  ledger. The artifact's 研究分桶 card becomes 地板对照 on the same principle:
  the buckets it names by hand belong to a strategy that no longer runs, where
  the measurements-against-frozen-floors reading stays true.

  An unanswered read is never an answer. `perspective == null` and an empty case
  table each carry three states — the lane opened no case, the batch has not
  come back yet, the batch failed — because rendering all three as the first
  makes the strongest possible positive claim exactly when the page knows least.
  The two halves of that batch also have different windows: Cases are bounded at
  `window_hours`, active Intents are unbounded in time by design, so neither
  section calls the batch 「这个窗口」 without naming both.

  Both sections name every endpoint they read in their `NewsSourceLine`. 交易视角
  joins the Case to its frame, so it declares `/api/trading/intents` *and*
  `/api/news/feed → events[].oi`; the identity band declares the two endpoints
  its three tiles come from beside the one its contracts do. A card that names
  one of two sources points an auditing reader at a response that does not carry
  the figure.

  When the quadrant is 象限不明, the panel names the case's own frozen
  `contexts.regime.reason`, published beside `pre_move_bps` and
  `strategy_config` as the projection's third named manifest slice. Not
  `policy_reason`: that is the strategy's later answer and is null on a Case the
  strategy went on to trade — which is exactly the traded-with-unclear-quadrant
  population the smart-money lane creates whenever a move sits between the
  shared 600 bps ceiling and its own 1000. Reading it there told an operator the
  ledger had recorded no reason, over a manifest that had recorded one.

  The Case column carries `case_state` for both halves of the batch. Case and
  Intent execution states are disjoint vocabularies: the next columns separately
  show Intent identity/state and the proven Outcome, so neither is mislabeled as
  a Case transition.

  Two things the artifact draws that this lane cannot answer. `thesis_zh` and
  `invalidation_zh` are not written by a pure rule — the same finding #256
  recorded for the Case surface — so the row expansion names the rule instead of
  paraphrasing a sentence nobody wrote. And a token the lane never opened a case
  for gets no reading at all: 「never asked」 and 「asked and found nothing」 are
  different answers, and four unlit quadrants would assert the second.

  Every `base_symbol` on the console routes here (#207 principle 9): the asset
  chips on feed rows, the drawer and the Event detail, and the collapsed
  identity in the Event's 符号归一 block. The feed row also carries the fixed
  1H/4H Event Reaction in its own column, on held rows as well as pushed ones —
  the verdict and what the market did are two different claims, and "the
  pipeline dropped it and it moved 3%" is the one thing the conclusion cannot
  say. A horizon that has not matured reads `未到期`, never `0.00%`.

  `/trading` is the Alpha/Execution operator desk: **three blocks over two
  endpoints, plus a Case drawer that opens on demand** (#537 PR-5). It was six
  blocks over four; the two that went were both funnels — a card of today's
  admission configuration beside a status distribution, and a list of every Case
  in the window whose only interactive purpose was opening one of them.

  1. **RISK** — `/api/trading/status`. One strip of `ALIVE`, `SAFE`, `ARMED`,
     `FLAT` with the blocking reason through `ENTRY_BLOCK_REASON_ZH` and
     `execution.routes_count` as `Runtime 可执行市场 N 个`; then equity, UTC-day
     drawdown, aggregate risk, private reconcile age and `audit_healthy` with
     its own failure reason; then the positions the venue holds, each with
     quantity, entry, mark, unrealized PnL and its protection trigger and
     coverage, under a header of the three order counts (open / inflight /
     unknown) and the protection word. Those three counts were a card of their
     own between the equity figures and the positions, where they read as a
     fifth safety answer; they are three integers about the same account.
  2. **ACT** — `POST /api/trading/execution/commands` to write, and the
     `commands[]` of `/api/trading/executions` to read back. Pause, Resume /
     Arm and Flatten, with **no confirmation dialog**: none of the three can
     submit an entry, and a modal in front of them taught readers that clicking
     through it was the dangerous act. `execution.mode=disabled` locks all
     three; all three write with the session token the page already holds (the
     pasted write token and the Live `CONFIRM` re-entry went with #520 PR-B). A
     successful POST says only that the Command was persisted. Each Command row
     is action, the server's `stage` — `recorded / accepted / rejected /
     completed / expired`, derived from `control_disposition` alone — and its
     clock. The reason column repeated the text the operator had just typed into
     the field above it, and `operator_identity` was the constant
     `operator-console` on every row a browser wrote.
  3. **CONFIRM** — `/api/trading/executions`. One row per entry: time, `source`
     (Signal or the operator's own manual entry), market, direction,
     `disposition_reason` through `SIGNAL_DISPOSITION_ZH`, `stage`, fill
     quantity and average price, stop trigger, exit price, realized PnL and exit
     reason. A manual entry renders beside a Signal with the same columns and no
     Case identity (#528 PR-3); a Signal row's `source` cell is the link that
     opens its Case. The two columns #537 PR-5 dropped were both second answers:
     `disposition` was `accepted` / `rejected` beside a `stage` that already says
     `ordered` or `rejected`, and `position_status` was `closed` beside
     `stage=closed`. The header totals entries, splits them by source, and sums
     the realized PnL, which is the page's only arithmetic — #528 refuses an
     equity-curve table for a number that is already one column.

  **The Case drawer** is `/api/trading/cases`, opened by `?case=<id>` — the deep
  link the desk's own Case rows publish. It shows one
  Case's terminal answer, the frozen per-check evidence and the frozen policy
  configuration, and says so when the Case is outside the 24 h window rather
  than rendering nothing. Beside it, one card carries that read's two durable 24 h
  distributions: `state_counts_24h` and the policy-reason counts. There is no
  pagination and no cursor — the response published a `next_cursor` no reader
  ever sent back.

  **One empty-ledger vocabulary.** Every ledger on the page says the same three
  sentences about its own subject word: reading, unreadable, or empty. Three
  blocks each carried their own copy of them and the failure banner named the
  same ledgers again in a fourth.

  When Decision is disabled, empty ledger copy says the lane has no work; it
  never rebrands execution as paper. Loading, cold failure, stale refresh, and a
  genuinely empty batch remain different page states. The responsive desk uses
  cards at desktop, tablet, and phone widths; CONFIRM's twelve-column table
  scrolls inside its own panel and never widens the document.


  The Event detail carried an admission badge until #553 PR-1 and carries none
  now. Only a deterministic source key was ever reconstructible from an
  `event_id`, and the deterministic lane no longer produces Events at all — a
  market observation is a stored fact — so the badge could answer for nothing
  and rendered null on every Event that still exists. It went with its query,
  its vocabulary and its fixtures rather than staying as a component that draws
  nothing.

  Polling: Feed every 3 seconds; 市场事实's group list every 10 seconds; Quote,
  Event detail, Status and the trading reads every 15 seconds (one shared News
  status query feeds the Feed header, the topbar health lamp, 市场事实's ingest
  strip and `/news/status`). The three Trading reads run on `/trading` alone
  (#553 PR-1): the shell polled `/api/trading/status` on
  every route for a sidebar badge and two chrome figures until #537 PR-5 deleted
  both. Token identity and one expanded market group do not poll.
  Quote batches preserve Feed order while deduplicating, select the first 100,
  and only then sort that selected identity for the request/cache key. Their
  interval pauses in a background tab and `refetchOnWindowFocus` immediately
  revalidates on return. A stale server quote keeps its number with a visible
  `陈旧 Xm` marker and three-clock tooltip. A failed poll with same-session LKG
  keeps the cached number but dims it and shows
  `行情读取失败 · 上次成功于 …`; a cold failure uses the shared error surface.
  Neither client condition invents a fifth quote state.
  Feed, Event, and Status retain ETag revalidation and a `304` reuses the
  cached body. There is no archive, revision timeline, read state, favorites,
  subscriptions, per-Event AI panel, push inbox, notification settings,
  browser model call, or adjustable threshold.
- **Page state.** Only an active first HTTP request may show Loading.
  Bootstrap pending/error, disabled query, transport error, same-session stale
  cache, and typed module-unavailable states use distinct `PageState.*`
  surfaces with a truthful retry/recovery action. A query disabled while the
  bootstrap token is missing must never leave an infinite skeleton.
- **CSS ownership.** `main.tsx` imports application-global CSS entrypoints only from `src/styles/`; that directory may organize those styles through local imports and nested files, but it may not become a bucket for component or feature selectors. New application-wide utility classes use the `tf-global-` namespace. Feature and shared UI selectors are imported by the component or route that owns them. Shared primitives such as `IconButton`, `PageState`, and `RouteBackLink` own their CSS under `shared/ui/`; feature CSS may lay out the containing toolbar or deck but must not redefine primitive internals. Do not use `.module.css` files as global selector buckets; CSS Modules must bind local classes from TypeScript.
- **CSS architecture harness.** `web/tests/architecture/cssArchitectureHarness.test.ts` is the future-proof gate for CSS ownership. Global entrypoints imported by `main.tsx` must live under `src/styles/`; the harness does not require every file there to be imported directly. Component and route CSS must live beside its owner. The harness rejects feature CSS that redefines shared UI classes, feature selectors outside their namespace, naked modifier classes such as `.active` or `.gap`, side-effect class names reused across feature roots, literal or locally derived colours outside `styles/tokens.css`, raw type sizes and radii outside the global scale, and unresolved custom properties. It does not preserve retired filenames. When a new feature needs side-effect CSS, add an explicit namespace policy there rather than borrowing another feature's selectors.
- **Rendered geometry.** Responsive navigation, overflow, landmarks and interaction are protected in Playwright, where computed layout exists. The explicit four-viewport visual lane remains diagnostic rather than merge evidence. Source tests do not pin selector spelling, file layout, exact track strings or a CSS line budget, so equivalent refactors remain possible.
- **Cascade layers.** Side-effect CSS participates in the app cascade contract declared in `styles/tokens.css`: `app.base`, `app.primitives`, `app.shell`, `app.features`, then `app.overrides`. `styles/base.css` uses `app.base`; shared primitives use `app.primitives`; cockpit shell files use `app.shell`; feature route CSS uses `app.features`. Unlayered side-effect CSS is allowed only for Tailwind's import file.
- **Responsive CSS contract.** Mobile behavior is a tested architecture surface, not a best-effort visual tweak. Shell CSS owns `.cockpit-shell`, `.cockpit-main`, `.center-column`, `.topbar`, `.topbar-sidebar-trigger` and `.cockpit-app-sidebar`, split by owner files (`cockpitShell.css`, `CockpitTopbar.css`, `AppSidebar.css`, `AppBottomNav.css`, and `cockpitShellContract.css`). Final shell breakpoint decisions, including the mobile topbar row height token, live in `features/cockpit/ui/cockpitShellContract.css`. Tablet route navigation is the shared `Drawer` primitive opened from the topbar trigger; below `768px` there is no drawer at all and `AppBottomNav` carries every destination (#87).
- **Route controls.** Shells do not render route-specific filter controls. News controls belong to the feature route that consumes them. `CockpitShell` is the only shell; it owns navigation, frame layout, and the main route scroll container.
- **Health lamp.** Pipeline health is one control in one place: `HealthLamp`
  inside `CockpitTopbar`, beside the page title, on **every** route and in every
  health state (#207, #256). It renders whenever `/api/news/status` answered at
  all, and `null` only when it did not — 流水线状态 holds no navigation slot, so
  hiding the affordance while healthy would make that page unreachable exactly
  when a reader wants to confirm nothing is wrong. The `topbar-health-lamp` button surface never
  changes size or copy: it reads `流水线` in every state, and its 7px dot carries
  `ok` / `warn` / `bad` / `off`. The worst item's own `summary_zh` reaches the
  reader through the accessible name, the `title`, and the popover — not by
  rewriting the button, which would make the topbar reflow on a poll. A failed
  read is its own `bad` state with a headline and a door but no stage lines. It is a `<button>` whose accessible name is
  `流水线健康：{summary_zh}`; opening it shows the four server stage lines
  (`接入 / 队列 / 模型 / 推送`, each `level` + `summary_zh`), the server instrument snapshot as a neutral
  `标的表` fact without a browser-invented level or sentence, and a link
  to `/news/status`. The popover is Radix's, so `Esc`, the dismiss layer and
  `aria-expanded` are the platform's and no `keydown` listener is added. Below
  `1279px` the sentence collapses to the dot; below `768px` the lamp is the one
  part of `.brand` that stays, because a phone reader would otherwise never learn
  the pipeline is degraded. The lamp reads the same
  `useNewsStatusWithToken` query key as everything else — no second poll — and
  the level, every stage level and every sentence are server values; the frame
  computes no health state of its own.
- **Icons.** Two families, one specification. Generic actions stay on lucide and
  are never redrawn (`Search`, `PanelLeft`, `ChevronDown`, `ChevronRight`, `Check`,
  `X`, `ExternalLink`, `SlidersHorizontal`). The product's own nouns are drawn in
  `shared/ui/icons.tsx` on the same 24 grid with a 2px round-capped stroke and
  `currentColor` only, each a `forwardRef` with lucide's exact signature so
  `AppNavigationItem.icon` stays typed `LucideIcon`: `EventStreamIcon`,
  `LeverageGaugeIcon`, `TradeFlowIcon`, `TelemetryPulseIcon`, and the three OI
  measurement glyphs `WhaleShareIcon` / `WindowClockIcon` / `ThresholdIcon`. The
  set holds exactly what is rendered — a glyph nothing imports is a claim about
  a surface that does not exist. An
  icon has three colours, all inherited — `--text-subtle` at rest,
  `--accent-primary` when current, `--text-faint` when disabled — and **never**
  red or green: those two hues state a market direction, which an icon never has.
  `TelemetryPulseIcon` and `LeverageGaugeIcon` never lean up or down, because
  open interest rising is not price rising (#104). Only `favicon.svg` and
  the sidebar's `BrandMark` may be filled shapes; they are the same path on the
  same indigo tile, so the tab and the frame are one face.
- **Shell navigation.** `AppSidebar` is a purpose-built 204px aside — one component for the in-frame sidebar and the drawer body, so the two presentations cannot disagree about what exists or which destination is current. `CockpitShell` picks the frame by mounting, not by hiding: from `(min-width: 1280px)` the sidebar is in-frame and stays there. From `768px` to `1279px` the same sidebar is the left `Drawer`; below `768px` `AppBottomNav` takes over. The nav carries three working surfaces in one `Workbench` group — `事件流` `/news`, `市场事实` `/news/market`, `交易` `/trading`. `System · 数据健康` held one entry and went with it (#553 PR-1): 市场事实 is a reading surface for what the venues reported, not a frame-parse audit, and whether the pipeline is telling the truth is the topbar lamp's question on every page. The feed entry shows the 24 h received count; the other two carry none — `/api/news/status` reports no market intake any more, and the destination prints the per-kind figures itself. A count clipped the 204px row's label to one glyph (#460), and the `tradingEnvironment` badge that replaced it — the lane's last-Case clock and the execution mode — cost every News route a 15 s poll of `/api/trading/status` for two words the desk itself states first (#537 PR-5). Counts are compacted and `aria-hidden`. `/` redirects to `/news`; topbar search always opens a fresh News scope. Public SPA routes are `/`, `/news`, `/news/market`, `/news/status`, `/news/symbols/:base`, `/news/events/:eventId`, and `/trading`; retired routes, including `/news/oi`, `/news/alpha` and `/news/leverage`, resolve through the standard not-found route. Operational diagnosis remains on API/CLI surfaces and there is no browser Ops route.
- **No keyboard layer.** The console has no command palette, no `?` shortcut panel, and no document-level key bindings at all; #82's keyboard layer was cut whole. Every action the palette collapsed — the three destinations, the four feed task tabs, a `symbol` filter — is already a control on the page, so the layer bought a second way to reach what one click reached and a list that had to be kept in sync with the routes; the toolbar was even advertising an `X 复制标注` binding that nothing implemented. The cut removed `shared/ui/CommandPalette`, `shared/ui/ShortcutsDialog`, `features/cockpit/ui/appShortcuts.ts` and `features/news/state/useFeedCursor.ts` together with the shell's own `keydown` listener, the `--surface-cursor` token and every `<kbd>` hint. Do not reintroduce a `document.addEventListener("keydown", ...)` in shell or route code, and do not restore the `⌘K` topbar button: keyboard access is the platform's — real controls, real tab order, `Enter` on a form, and Radix's own `Esc`.
- **Scrolling.** `body` remains locked for the app shell. `.center-column` is the shell-managed route scroll container. No retired table, bottom deck, controls row, or mobile task-bar reserves height. Route-level nested scrollers are allowed only when they are intentionally bounded and covered by Playwright overflow/reachability assertions.
- **Breakpoint policy.** Desktop density starts at `1280px`. Tablet uses a single route column from `768px` through `1279px`. Mobile rules are `max-width: 767px` and must appear late enough in the cascade to win over base and desktop/tablet rules. Use container queries for local card/panel behavior when component width matters more than viewport width.
- **Side-effect CSS review signal.** Large owner stylesheets are a cohesion signal
  for review, not a correctness threshold. CSS ownership and forbidden
  cross-owner selectors remain mechanical boundaries; equivalent selector,
  variable or file refactors are judged by rendered geometry, overflow,
  accessibility and the explicit visual lane rather than an exact line budget.
- **Accessibility.** Icon-only controls use `IconButton` with an explicit `aria-label`; route status regions use polite live regions; form controls need visible or screen-reader labels. `jsx-a11y/recommended` is enforced as an error gate.
- **Colour axes.** Colour carries exactly two axes and they never share a hue.
  *Market direction* owns red and green — 红 = 利多, 绿 = 利空, the mainland
  convention (`--dir-bullish` / `--dir-bearish`, `directionTone`). *Pipeline outcome*
  owns blue, amber and grey and must never use red or green (`--signal-done` /
  `-info` / `-caution` / `-alert` / `-neutral`, `outcomeTone`): 已推送 is a completed
  step, not a market opinion, and colouring it green would read as a second,
  contradicting 利空. Every foreground token clears 4.5:1 on white. The direction red
  and green sit at near-equal luminance by necessity, so the arrow glyph and the
  Chinese word carry the meaning and colour only reinforces it.
- **Shared primitives.** `Card`, `Metric`, `Bar`, `FactGrid`, `KeyValue`,
  `ActionButton`, `IconButton`, `PageState`, `Toast` and `Drawer` in `shared/ui` own
  the console's panel, figure, proportion, labelled-fact, key/value, button,
  page-state, confirmation and sheet shapes. A feature may frame a primitive with its own class but must not
  restyle one — not even to hide it at a breakpoint; wrap it in a feature-owned element
  instead. `cssArchitectureHarness` enforces this. Use the component: hand-writing its
  class names leaves the primitive's stylesheet out of the bundle entirely, which is
  exactly how the verdict grid and every key/value table lost their layout before #82.
  The console does not depend on a component framework — the shadcn `sidebar`, `sheet`,
  `tooltip`, `input`, `separator`, `tabs`, `panel`, `alert`, `button` and `skeleton`
  wrappers were deleted with the v6 redesign because nothing rendered them any more.
  Radix is used directly, and only for the one thing that needs a focus trap and a
  dismiss layer: `Drawer`.
- **Phone reading.** The console has to be comfortable to read on a phone. The shell
  sets `viewport-fit=cover`, a `theme-color` matching the canvas, `format-detection`
  off, `text-size-adjust: 100%` so a reader's larger font scales coherently, `100dvh`
  so the shell does not jump as mobile toolbars slide, `env(safe-area-inset-bottom)`
  on the route column, and a CJK-first font stack. Below `767px` the funnel tiles
  compress to one scrollable row and the page subtitle is dropped so the first Event
  sits within the first screen. Padding must never land on a `-webkit-line-clamp`
  element — the clamped line shows through the padding band.
- **Score display.** Displayed scores and labels (Triage magnitude and confidence, the News `outcome`/`*_zh`/`label_zh` copy) are server-owned values rendered as-is. The UI does not recompute, rank, translate, or synthesize them locally; `features/news/newsLabels.ts` holds UI affordance copy and tone mapping only.
- **No token or provider-image surfaces.** There is no token profile, logo, chain/address link, DEX/CEX market panel, or image proxy anywhere in `web/src`; the API exposes no image URL or image route. Do not add a frontend proxy, helper, or filter that loads or rewrites provider image URLs.

## Build And Test

Common frontend gates:

- `cd web && npm run lint`
- `cd web && npm run test:architecture`
- `cd web && npm run typecheck`
- `cd web && npm test -- --run`
- `cd web && npm run build`
- `cd web && npm run test:e2e` (explicit four-project visual/interaction lane)
- `make test-browser-smoke` (required single-Chromium FastAPI/browser seam)

Playwright projects are part of the frontend contract:

- `desktop-1366` (`1366x720`)
- `desktop-1920` (`1920x1080`)
- `tablet-834` (`834x1194`)
- `mobile-390` (`390x844`)

Desktop-only specs must explicitly skip non-desktop projects. Mobile-only specs must explicitly skip non-mobile projects. New `page.setViewportSize` calls are allowed only in dedicated responsive specs or explicitly marked desktop-only specs.

The required smoke uses a separate single-Chromium project with no route
interception and no skips. It loads the production bundle from FastAPI,
observes `/api/bootstrap`, verifies the installed bearer reaches
`/api/news/feed`, and renders a service-owned Event fact on `/news`. Every
Playwright spec uses the shared guard fixture: unexpected `pageerror`, console
error, failed request or unhandled API request fails the case. The four-project
mock/visual lane remains valuable for responsive interaction and screenshots
but is not evidence of a backend seam and is not required on every PR.

Required Vitest runs set `allowOnly=false`, disable retry/repeat, and emit the
built-in JSON report under `artifacts/test-results/`. Required-test ESLint
policy rejects focused, disabled, expected-failure, retry, and repeat syntax.
Every required `test`/`it` declaration uses its unaliased named binding directly
(including `.concurrent` and parameterized `.each`/`.for` cases) and has the
fixed shape `(case name, callback)`. Namespace/dynamic imports, copied or
extended bindings, computed modifiers, and options arguments are forbidden.
This keeps an indirect or runtime-built expected-failure option from turning a
real assertion failure green. The binding/declaration rule covers every
`web/tests` helper and support module, not only top-level specs. The sole shared
Playwright fixture factory is separately constrained to export exactly
`test = base.extend(...)` and cannot register a case;
the small native-report guard then requires a non-empty run with no failed,
pending/todo, retried, snapshot-mutating, module, or unhandled outcome. It does
not replace or reinterpret Vitest. Playwright likewise emits native JSON and
the required smoke remains a non-empty, no-skip, no-retry run. Pytest's `slow`
marker owns deliberate fault injection across real Vitest/Playwright native
runs, the runtime-error guard, and the required-test ESLint policy; these
nested frontend checks do not run from the architecture lane or `make check`.

Repository hermetic bundle when the changed seam requires it:

- `make check`

Focused development runs select the exact Vitest and affected lint, type,
build, browser, or visual seam per `DEVELOPMENT.md`; localized frontend changes
do not run unrelated backend lanes. `make test-ci` is an optional complete
local preflight only for declared high-risk changes. The successful fixed
GitHub Actions `ci-gate` for the exact PR HEAD is merge authority; the exact
main SHA's fixed workflow is release/deploy evidence. The visual matrix and
scheduled diagnostics remain explicit separate lanes.

Production bundles ship inside the same Docker image as the Python service and are served by the FastAPI static-file mount.

## UI Verification Gate

Per `DEVELOPMENT.md`, UI flows that tests cannot exercise must be checked manually before declaring completion. The minimum checklist for frontend architecture changes is:

1. Hard-reload `/`, `/news`, `/news/market`, `/trading`, `/trading?case=<id>`,
   `/news/status`, `/news/symbols/:base` and `/news/events/:eventId` with representative query
   params; confirm `/news/oi`, `/news/alpha`, `/news/review`, `/macro`, `/search`, and `/token/...` render the
   not-found surface. On the token page, confirm a base no venue lists (`/news/symbols/SPOT`)
   says so rather than erroring, and that `/news/symbols/xyz-wif` resolves to the
   same page as `/news/symbols/WIF`.
2. Submit the topbar search from `/news/status` and from `/news` and confirm
   the URL becomes `/news?q=<submitted-query>&outcome=all&hours=168`; existing
   News filters and cursors do not survive this new search scope.
   The box has no submit button: `Enter` submits, and the visible `/` keycap is inert on every route.
3. Verify visible loading/empty/error states are structured, labelled, and non-overlapping.
4. Confirm no failing `/api/*` requests and no WebSocket connection attempt in the browser session.
   On `/trading`, verify disabled controls; alive-but-unsafe and safe-but-paused
   states; a protected position; pending/failed protection; an unknown order; a
   Command at each of `recorded / accepted / rejected / completed / expired`; a
   Signal row at `rejected`, `expired` and `closed`; and the four safety words
   reading `过期` once `facts_expire_at_ms` has passed. Confirm Resume and
   Flatten write on one click with no dialog, and that every success message
   still denies Runtime/venue completion.
5. Confirm the topbar shows no status pill while `/api/status.runtime.ok` is
   true and shows the first runtime reason when it is not, and that the feed
   header shows no health pill while `health.overall` is `ok`.
6. At `390px`, confirm there is no sidebar trigger, the bottom tab bar shows every destination with 48px targets and clears the home indicator, `.topbar` / `.center-column` / the bar do not overlap, Event rows read as separate cards with no select box and no expand caret, the funnel tiles and task tabs scroll horizontally inside themselves without giving the page a horizontal scroll, `/` lands on the News list, the approved tabs/time/filter controls remain reachable, and no retired Tape/task bar exists. On `/trading`, confirm the RISK strip, the account figures, the positions and their order-count header, the ACT controls and Command rows, the CONFIRM table (which scrolls inside its own panel) and the 24 h Case card remain reachable without page-level horizontal overflow.
7. At tablet width around `834px`, confirm the desktop sidebar is not mounted, the topbar trigger opens the drawer, drawer route navigation and topbar search still work, and the News list and no-overflow contract remain intact.
   At `1280px` and above, confirm `/news` keeps the sidebar fixed in the frame with no trigger, other routes
   retain the shared fold trigger, all three destinations are present and 交易 carries its mode word,
   `/news/events/:eventId` keeps `事件流` current, and the feed count matches
   the funnel's `收到`.
8. Confirm the keyboard binds nothing on `/news`: `⌘K`, `?`, `J`, `K`,
   `1`–`4`, `G`→`F` and `/` do nothing outside a text field, `Space` still
   scrolls the page, and the topbar carries no 命令面板 button or `⌘K` hint; its visible `/` keycap remains inert.
   `Tab` still reaches every control in order, and `Esc` still closes
   the drawer because Radix owns that.
9. From a row at `≥1024px`, confirm a plain click opens the compact drawer with the
   list still visible and the URL unchanged, clicking the next row swaps the
   drawer's Event without closing it, the footer's primary-token link reaches the token page, and `打开事件详情` reaches
   `/news/events/:eventId`. There, confirm `上一条`/`下一条` and `i / n` walk the
   same filtered list; then paste the URL into a fresh tab and confirm the
   pager is absent rather than broken.
10. At `1920px`, `1366px`, `834px`, and `390px`, verify the default News Feed
    requests latest 25-row pushed pages for the last 24 h with no direction or channel filter; `q`, `outcome`,
    `hours`, comma-separated `direction`, and comma-separated `channel` survive reload and alter server results;
    the channel control exposes exactly 新闻 / 上币/下币 and every feed/detail kind badge reads
    `event_kind` rather than admission, title or verdict type;
    the header shows the 24 h funnel card; the four task tabs show
    counts that match the rows each tab lists and follow a changed window or
    filter; every row shows time, headline and meta line; a pushed / pending /
    failed row shows exactly one outcome badge with Chinese copy and a coloured
    rail while a held row shows only its grey `reason_zh` — no rule, admission,
    decision, or score keys anywhere; and every row with a verdict shows the
    direction chip (利多 filled red / 利空 filled green / 中性 quiet text, each
    with its own arrow). On `/news/events/:eventId`, verify hero (outcome +
    reason, headline, direction + magnitude + 把握, why, the
    taxonomy `事件族/变化状态/来源权威/断言状态/主题` followed by the diagnostic
    `旧分类` and `SCOPE/NOVELTY/ACTIONABLE/AUDIENCE/MEMBERS` grid with framed cells,
    主要标的 vs 提及), the timeline with `+Δ` and an end-to-end figure,
    同类报道 and a collapsed 技术详情 appear in that order
    with no market-mark table — the two #88 market blocks (`当前报价` and
    `事件后反应`) are separate cards, never one table, because a rolling change and a
    fixed post-Event return are different time semantics; the hero's left rail carries the direction colour and a
    neutral verdict leaves it uncoloured; an Event with no Triage verdict renders
    the hero without the 判定 block instead of empty cells; and the back link
    returns to the feed the reader came from. Verify `/news/status` shows four
    coloured health cards with bars, the overall pill, the funnel with its
    biggest-drop sentence, Chinese reason bars,
    no buttons), the Strategy usage bar with counts only (never IDs), a
    collapsed 技术指标, and no operator controls. Confirm the technical key/value
    tables render as two-column grids rather than stacked `dl`s. Confirm about
    two News rows remain scannable at 390px and at least four at desktop height
    without horizontal overflow.
11. On `/news`, let the page cold-load and confirm the 2px in-flight line at the
    top of the viewport, the five funnel tile bones, and the six row bones
    fading with depth — then confirm all three are gone once the reads answer
    and that nothing on the page changed height. Confirm the hour strips read
    `HH:00 — HH+1:00` with the run's own count, that a 7-day window prefixes
    the day, and that switching tabs or filters regroups without reordering.
12. From `/news` with a non-default filter and from `/trading`,
    open a `base_symbol` and confirm the token page's back link names the page
    you actually left and returns you to it with its query state intact — the
    referrer travels as route state (`shared/routing/routeReferrer.ts`), and a
    cold token-page URL correctly falls back to 事件流.
13. At `1920px`, `1366px`, `834px`, and `390px`, seed a stale quote and confirm
    its number remains visible beside `陈旧 Xm`, the tooltip names venue plus
    provider/receipt/reference clocks and ages, and the dense Feed has no page
    overflow. Then fail a quote refetch: with LKG, confirm the cached number is
    visibly degraded under `行情读取失败 · 上次成功于 …`; without cache, confirm
    the shared loading/error surface renders. Background the tab long enough
    to pause interval polling, return, and confirm one immediate quote refetch.
