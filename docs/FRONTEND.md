# Frontend

> **Scope.** Owns the `web/` architecture, layer responsibilities, component conventions, and the UI verification gate. Backend layer boundaries live in `ARCHITECTURE.md`; public HTTP/WebSocket contracts live in `CONTRACTS.md`; install and run commands live in `SETUP.md`.

## Source Layer Map (`web/src/`)

| Directory                | Responsibility                                                                                                                                                                                                    |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/`                   | Application composition: providers, router wiring, top-level error boundary, and route fallback. It may compose feature route elements, but it must not own feature data queries or business rendering.           |
| `routes/`                | Route entries and URL-state orchestration. Route modules parse/serialize shareable state and choose the owning feature view.                                                                                      |
| `features/<name>/api/`   | Feature-owned endpoint adapters, query keys, and reusable server-state hooks. Feature public hooks/controllers may own narrow server reads when they are the feature boundary consumed by routes or UI.           |
| `features/<name>/model/` | Pure feature helpers, view models, and constants. Framework-free where practical.                                                                                                                                 |
| `features/<name>/state/` | Local client state that is not shareable URL state and not server cache state. Keep it narrow and feature-owned.                                                                                                  |
| `features/<name>/ui/`    | Feature screens and components. UI reads data from props or feature hooks exposed through the feature public index, not from another feature's deep files.                                                        |
| `shared/query/`          | Cross-feature React Query primitives, query-key helpers, and cache patching utilities.                                                                                                                            |
| `shared/routing/`        | Reusable route parsing, path building, and URL search-param helpers.                                                                                                                                              |
| `shared/socket/`         | WebSocket provider, route-aware subscription registry, and socket test helpers.                                                                                                                                   |
| `shared/ui/`             | Reusable presentational primitives and cross-feature token display components. No server fetching.                                                                                                                |
| `lib/api/`               | Typed HTTP client facade and auth-token plumbing. No feature query hooks.                                                                                                                                         |
| `lib/env/`               | Runtime environment parsing.                                                                                                                                                                                      |
| `lib/types/`             | Generated OpenAPI types and frontend-owned view contracts.                                                                                                                                                        |
| `styles/`                | Global Tailwind import, design tokens, and base element styles only. Feature/page selectors belong beside their owning component or feature as side-effect CSS, or as real CSS Modules with local class bindings. |

Do not add new code under old `api/`, `store/`, or `components/` roots. Public feature imports should come from `@features/<name>`; sanctioned route-shell entrypoints may use `@features/<name>/shell`. Deep imports across feature internals are blocked by lint and grep gates; the relative-import boundary gate derives feature roots from `web/src/features`.

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
| `socket/`           | Socket snapshot and subscription test utilities.                                                       |
| `e2e/golden-paths/` | Playwright browser golden paths.                                                                       |

## Conventions

- **Design contract and page archetypes.** Tracefold has one dark, restrained
  operator-workbench language. `styles/tokens.css` is the only semantic color,
  type, radius, focus, and shell token contract; production code must not add a
  parallel theme or compatibility alias. Stable routes declare one of three
  information archetypes with `data-page-archetype`: `scan` for the News
  Event feed and News status; `case` for Search, Token Case, and News Event
  detail; `decision` for Macro. The archetype governs hierarchy and density, never data ownership or
  business inference.
- **Data ownership.** Feature-owned API hooks, page hooks, and controller hooks own server reads/writes. Route modules and presentational UI components consume those feature hooks and must not call `useQuery`, `useMutation`, `useInfiniteQuery`, `getApi`, `postApi`, or `queryClient.set*` directly. `frontendDataOwnership.test.ts` enforces this boundary for `web/src/routes` and `web/src/features/*/ui`.
- **URL state.** Shareable Search and Token Case options live in their owning route-state helpers. No route carries hidden same-session scroll or selection state that would not survive a hard reload.
- **Socket lifecycle.** `shared/socket` owns authentication, Token Case live-market cache patches, and ref-counted market-target subscriptions. The React client sends `replay: 0` and does not retain public `event` messages; backend event/replay remains a public evidence contract for other consumers. News and Macro register no market target and are never patched from WebSocket state. Token Case subscribes only its active target. Stream/poll workers emit live market messages only after durable current-row persistence; those messages remain a cache enhancement, not a second source of truth.
- **Search route.** `/search` reuses the cockpit topbar but owns its
  search-local rail, filters, resolver candidates, and selected result. Topbar
  submit navigates to `/search?q=<query>`. Token search results render the
  shared Token Case panel directly from `/api/search/inspect`; they do not
  fetch `/api/token-case` again. Token, topic, and ambiguous results render
  resolver, identity, source-post, and market facts only.
- **Token Case route.** `features/token-case` owns persistent
  `/token/:targetType/:targetId` inspection. The route parses `window` and
  optional trigger focus from the URL, fetches `/api/token-case`, seeds
  `/api/target-posts` from the dossier's first page, and subscribes only the
  active target for live market updates. The dossier renders identity/market
  facts and raw posts without synthesizing prose or per-post conclusions; it
  has no token profile card, logo, or profile links, and the single GMGN action
  link is derived from the target chain and address. An
  inbound link may carry `window`, `focus=trigger`, and a canonical
  `trigger_event_id`; the route locates and visually focuses that exact Event
  or states that it is unavailable. It does not reconstruct any retired
  rank, lane, decision, or score. The header return action links back to
  `/search` for the current token; there is no other scan-surface return
  state.
- **News routes.** `/news` is a decision-first scan surface over the flat
  Event feed from `/api/news/feed`; the browser never clusters, scores,
  triages, throttles, or reorders. The public News navigation contains
  exactly `事件流` and `状态`, backed by `/news` and `/news/status`. Event
  detail lives at `/news/events/:eventId` and carries the same navigation plus
  a `RouteBackLink` to `/news`. The retired `/news/brief`, `/news/sources`,
  and `/news/stories/:storyId` routes, hooks, fixtures, and CSS are deleted;
  they resolve through the standard not-found route with no redirect. There is
  one operator-bound product and no reader-personalized or user-adjustable
  threshold. OpenNews admission is the exact configured account Strategy
  allowlist; the browser neither displays the private Strategy IDs nor
  reimplements provider rules, Gate admission, Triage, or storyline throttling.

  Feed query state is URL-owned and mirrors the server contract exactly:
  `q`, `family`, `admission`, `priority` (`high|normal`), `decision`
  (`push|escalate|drop|throttled|degraded`), `symbol`, and `sort`
  (`latest|priority`). Unknown `priority`/`decision` values are dropped rather
  than forwarded. The default request is `sort=latest` with `limit=25` and no
  hidden filter. Topbar search on News routes writes `q`; it does not call
  `/api/search/inspect` or reuse token resolver state. The backend searches
  and filters before cursor pagination. Active filters are removable chips
  beside one compact filter disclosure (family, admission, priority, decision,
  symbol). Pagination is an explicit `加载更多事件` action, loaded pages
  deduplicate by `event_id`, and there is no automatic infinite scroll or
  client-side time-window control. A refreshed first page inserts new Events
  at the top when the reader is already there; when the reader is scrolled
  away, the route preserves the viewport and shows a bounded new-item
  affordance that returns to the top.

  Feed rows (`NewsEventRow`) are compact reading cards over
  `NewsFeedEventData`. The first metadata line renders server-owned priority,
  Triage `final_decision` (or `待判定` when no verdict exists), admission,
  family, asset class, and the numeric `provider_score_max` badge (omitted
  without numeric evidence), followed by reporting origin, exact local
  `opened_at_ms` date/time plus relative time, and `member_count`. The Triage
  `title_zh` (or `leader_title` when the verdict has none) is the
  primary two-line headline and links to `/news/events/:eventId`; a differing
  `leader_title` appears as `原标题`, and a valid `context_line` is a two-line
  secondary line. The footer renders `grounded_assets` under `落地资产`
  (`未落地` when empty; `watchlist_hits` mark matching chips as `data-watch="hit"`),
  the Triage strip (`direction · M<magnitude> · event_type`, `headline_zh`,
  `override_rule`, `throttled_by`, and a `降级` flag), the delivery summary
  state with its settled time, and a separate original link when a valid URL
  exists. Missing values are omitted, never rendered as placeholder copy.
  Row tone (`data-direction`, `data-priority`, `data-decision`) is styling
  only. At 1280×720 the target is at least four rows, at 390×844 about two,
  with no horizontal overflow.

  `/news/events/:eventId` reads `/api/news/events/{event_id}` and renders
  six server-owned sections in fixed order: the Event hero (badges, the Triage
  verdict's `title_zh` (or `leader_title`) as `h1`, `原标题`, valid `leader_description`,
  storyline `context_line`, grounded assets, source/opened/last-member/
  published clocks, ingest mode, storyline key, macro lexicon, Event ID, and
  the representative original link); `members[]` cards with reporting origin,
  `match_kind`, `jaccard_estimate`, published time, title, valid description,
  item id, and original link; `NewsVerdictPanel` over `verdicts[]` (stage,
  final decision, degraded/error flags, rule baseline, model decision,
  `override_rule`, `throttled_by`, model/policy/prompt versions, publish time,
  the typed Triage or Analyst payload fields, verdict assets, context
  evidence, and a collapsed `trace` disclosure); `deliveries[]` rows (kind,
  state, error code, attempted/settled clocks, receipt entries); `labels[]`
  under `操作者标注` (source, label payload, created time, or an explicit empty
  state naming `tracefold news label`); and `marks[]` as a scrollable
  market-mark table.
  The browser does not recompute any verdict, decision, or delivery state.

  `/news/status` reads `/api/news/status` and presents the single server
  `state`/`workers_state`/`measured_at_ms` overview followed by four fact
  layers in fixed order — `ingest` (token, WSS connection, last frame/publish,
  last error, configured and provider-enabled Strategy counts, open incidents,
  strategy warnings), `broker` (configured, connected, error, per-queue
  messages/consumers), `pipeline` (1h/24h events, candidates, triage, degraded
  triage, deep, decided push, throttled, Triage P50/P95, triage/analyst
  models), and `delivery` (availability, 1h/24h sent, terminal, hourly cap,
  end-to-end P95, last error) — plus read-only `control` (paused, mutes) and
  the read-only watch symbol list (`news-watch-*`). No layer computes a second
  health state; only the server `state` badge is coloured. There are no
  pause/resume/mute controls, no source inventory, and no Brief.
  The Feed header renders compact reader-facing health from the same status
  query (`WSS 已连接/未连接`, `1h 事件`, `Triage P95`, `24h 推送`).

  Polling: Feed every 3 seconds; Event detail and Status every 15 seconds
  (one shared status query feeds both the Feed header and `/news/status`).
  Feed, Event, and Status retain ETag revalidation and a `304` reuses the
  cached body. There is no archive, revision timeline, read state, favorites,
  subscriptions, per-Event AI panel, push inbox, notification settings,
  browser model call, or adjustable threshold.
- **Macro routes.** `/macro` and `/macro/overview` render one compact index over
  the six current modules. `/macro/rates-fed`, `/macro/economy-inflation`,
  `/macro/liquidity-funding`, `/macro/credit`, `/macro/volatility`, and
  `/macro/cross-asset` are the only Macro detail routes and are backed by their
  matching `/api/macro/*` reads. They do not accept a generic window parameter.

  The overview shows transport state, latest fact time, each module's
  availability/currentness/coverage/history depth, and aggregate data quality.
  It does not synthesize a daily narrative, asset call, or historical session.

  Each module is a typed fact workbench. Its header shows as-of clocks and
  quality; hash-selected sections render only the server-provided facts,
  charts, tables, lineage, contradictions, falsifiers, and checkpoints that
  belong to that module. Empty semantic sections are omitted. Release modules
  distinguish expected, actual, surprise, revision, source publication time,
  and ingestion time. Dataset details keep data, market, and source state
  separate; optional history affects History Depth, not Current Health.
  Current blockers are expanded before the workbench. Historical partial-depth
  audit remains collapsed below the primary task; it must never push the first
  useful section thousands of pixels down the page.

  Rates begins with the persisted 2Y/10Y/30Y completed-session matrix,
  2s10s/10s30s, and aligned 10Y/30Y nominal-real-breakeven decomposition. It
  then renders maturity cross-sections, source clocks, the official FOMC
  meeting calendar, recent Treasury auction-demand facts, Fed institutional
  stance, officials distribution, and the event timeline. The optional Fed
  analysis runtime configuration is a separate `disabled`/`unconfigured`/`active`
  evidence lane. `active` means the worker's configuration admission conditions
  are satisfied; it is not a process-liveness signal. Disabled analysis does not
  make official Rates/Fed facts unavailable,
  and `no_call` never renders as a zero-score distribution. Bill discount rate,
  investment rate, and high yield retain distinct labels and nullable values.
  Economy releases show their Registry-owned seasonal-adjustment convention
  beside reference, publication, and ingestion clocks. Cross-Asset keeps the fixed
  ETF matrix, normalized comparison, futures, and USD-index facts distinct. Its
  normalized charts state that the comparison base is 100; a compact benchmark
  strip exposes each price/return source and as-of time. The correlation window
  selector is generated solely from the server's correlation contract, and the
  mirrored matrix/diagonal are display-only derivations.
  Credit keeps its four concurrent dimensions and no composite score.
  Volatility alone owns the official-expiry CFE VX settlement curve.

  The browser never calculates a Macro metric, merges source identities,
  chooses a fallback conclusion, invokes a model, or repairs persisted state.
  A missing module renders its typed unavailable reason without hiding the
  other five. At desktop, tablet, and mobile widths, content becomes labelled
  stacked sections without page-level horizontal scrolling or hover-only
  evidence. Dense correlation matrices may use one labelled local scroller.

  Module headers and the Dataset audit render server-owned group/Dataset
  health, exact reasons, affected Dataset IDs, source/effective/received
  clocks, recovery mode, and next-check time. A cached refetch failure is
  visibly stale instead of silently presenting the cached body as current;
  typed unavailable states retain their retry or operator-recovery action.
  Module reads poll every 60 seconds with a 30-second query stale time and use
  a stable per-module ETag cache key. Unchanged bodies reuse the cached typed
  response after `304`; transport failures keep the last body only behind the
  visible update-delayed state.
- **Page state.** Only an active first HTTP request may show Loading.
  Bootstrap pending/error, disabled query, transport error, same-session stale
  cache, and typed module-unavailable states use distinct `PageState.*`
  surfaces with a truthful retry/recovery action. A disabled token query must
  never leave an infinite skeleton.
- **CSS ownership.** `main.tsx` imports only Tailwind, tokens, and base styles. Feature and shared UI selectors are imported by the component or route that owns them. Shared primitives such as `IconButton`, `PageState`, and the case-file components own their CSS under `shared/ui/`; feature CSS may lay out the containing toolbar or deck but must not redefine primitive internals. Do not use `.module.css` files as global selector buckets; CSS Modules must bind local classes from TypeScript.
- **CSS architecture harness.** `web/tests/architecture/cssArchitectureHarness.test.ts` is the future-proof gate for CSS ownership. It rejects retired global buckets (`cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, `signalLab.css`), side-effect CSS imported from non-local owners, feature CSS that redefines shared UI classes, feature selectors outside their namespace, naked modifier classes such as `.active` or `.gap`, and side-effect class names reused across feature roots. When a new feature needs side-effect CSS, add an explicit namespace policy there rather than borrowing another feature's selectors.
- **Cascade layers.** Side-effect CSS participates in the app cascade contract declared in `styles/tokens.css`: `app.base`, `app.primitives`, `app.shell`, `app.features`, then `app.overrides`. `styles/base.css` uses `app.base`; shared primitives use `app.primitives`; cockpit shell files use `app.shell`; feature route CSS uses `app.features`. Unlayered side-effect CSS is allowed only for Tailwind's import file.
- **Responsive CSS contract.** Mobile behavior is a tested architecture surface, not a best-effort visual tweak. Shell CSS owns `.cockpit-shell`, `.cockpit-main`, `.center-column`, `.topbar`, and the shadcn sidebar composition (`SidebarProvider`, `AppSidebar`, `SidebarInset`, and `SidebarTrigger`) split by owner files (`cockpitShell.css`, `CockpitTopbar.css`, `AppSidebar.css`, and `cockpitShellContract.css`). Final shell breakpoint decisions, including the mobile topbar row height token, live in `features/cockpit/ui/cockpitShellContract.css`. Mobile and tablet route navigation uses the shadcn `Sheet` drawer opened from the topbar trigger.
- **Route controls.** Shells do not render route-specific filter controls. News, Macro, Search, and Token Case controls belong to the feature route that consumes them. `CockpitShell` and `SearchShell` own only navigation, frame layout, the main route scroll container, and route-appropriate hotkeys.
- **Shell navigation.** Desktop users navigate through the collapsible shadcn `AppSidebar`; tablet and mobile users open the same route tree through the topbar `SidebarTrigger` and shadcn drawer. The primary route tree contains exactly News and Macro in that order; `/` redirects to `/news`, and Search remains reachable through the topbar submit flow. Healthy runtime state is silent. Configuration, service, or realtime anomalies appear as an accessible topbar status; operational diagnosis remains on the API/CLI surfaces and there is no browser Ops route. Live has no route-local bottom task navigation. `/stocks` is removed and resolves through the standard not-found route; there is no redirect or compatibility screen.
- **Scrolling.** `body` remains locked for the app shell. `.center-column` is the shell-managed route scroll container. No retired table, bottom deck, controls row, or mobile task-bar reserves height. Route-level nested scrollers are allowed only when they are intentionally bounded and covered by Playwright overflow/reachability assertions.
- **Breakpoint policy.** Desktop density starts at `1280px`. Tablet uses a single route column from `768px` through `1279px`. Mobile rules are `max-width: 767px` and must appear late enough in the cascade to win over base and desktop/tablet rules. Use container queries for local card/panel behavior when component width matters more than viewport width.
- **Side-effect CSS budget.** Architecture tests fail any side-effect CSS file above 500 lines. Component-specific styling should move toward CSS Modules or smaller owner files instead of growing route-wide side-effect CSS buckets.
- **Accessibility.** Icon-only controls use `IconButton` with an explicit `aria-label`; route status regions use polite live regions; form controls need visible or screen-reader labels. `jsx-a11y/recommended` is enforced as an error gate.
- **Score display.** Any displayed ranking score includes its component breakdown from the API. The UI does not recompute ranking facts locally.
- **No provider images.** Token Case renders no token logo; the API exposes no
  image URL or image route. Do not add a frontend proxy, helper, or filter that
  loads or rewrites provider image URLs.

## Build And Test

Common frontend gates:

- `cd web && npm run lint`
- `cd web && npm run test:architecture`
- `cd web && npm run typecheck`
- `cd web && npm test -- --run`
- `cd web && npm run build`
- `cd web && npm run test:e2e`

Playwright projects are part of the frontend contract:

- `desktop-1366` (`1366x720`)
- `desktop-1920` (`1920x1080`)
- `tablet-834` (`834x1194`)
- `mobile-390` (`390x844`)

Desktop-only specs must explicitly skip non-desktop projects. Mobile-only specs must explicitly skip non-mobile projects. New `page.setViewportSize` calls are allowed only in dedicated responsive specs or explicitly marked desktop-only specs.

Repository fast gate:

- `make check`

Integration, backend E2E, golden, and browser lanes are selected explicitly
from the changed seam per `TESTING.md`; there is no monolithic repository-wide
completion target.

Production bundles ship inside the same Docker image as the Python service and are served by the FastAPI static-file mount.

## UI Verification Gate

Per `DEVELOPMENT.md`, UI flows that tests cannot exercise must be checked manually before declaring completion. The minimum checklist for frontend architecture changes is:

1. Hard-reload `/`, `/search`, `/news`, `/news/status`,
   `/news/events/:eventId`, `/macro`, and
   `/token/:targetType/:targetId?window=4h` with representative query
   params.
2. Submit the topbar search and confirm the URL becomes `/search?q=<submitted-query>`.
3. Verify visible loading/empty/error states are structured, labelled, and non-overlapping.
4. Confirm no failing `/api/*` requests in the browser session.
5. Confirm route-aware WebSocket subscription behavior: `/news` and `/macro`
   register no `market_targets`; Token Case registers only its active target and
   releases it after leaving the route.
6. Confirm Token Case renders identity, market, and source-post facts only,
   with the GMGN action link derived from the target chain/address and no
   browser request to a provider image URL.
7. At `390px`, confirm the topbar `SidebarTrigger` opens the shadcn drawer, drawer route links are reachable, `.topbar` and `.center-column` do not overlap, `/` lands on the News list, and no filter/Tape/task bar exists.
8. At tablet width around `834px`, confirm the desktop sidebar is hidden, the topbar trigger opens the shadcn drawer, drawer route navigation and topbar search still work, and the News list and no-overflow contract remain intact.
9. At `1920px`, `1366px`, `834px`, and `390px`, verify the default News Feed
   requests complete latest 25-row pages with no hidden filter; `q`, family,
   admission, priority, decision, symbol, and sort survive reload and alter
   server results; headline rows remain readable; priority, Triage decision,
   admission, asset class, the numeric OpenNews score, grounded assets, the
   Triage strip, and the delivery state are visible; exact date/time remains
   visible; current WSS state and pipeline latency are inline. On
   `/news/events/:eventId`, verify the hero, members, verdicts, deliveries,
   operator labels, and market marks appear in that order, `trace` starts
   collapsed, and the back link returns to `/news`. Verify `/news/status`
   shows the overview plus the four `ingest/broker/pipeline/delivery` layers,
   read-only control and watch views, and no operator controls. Confirm about
   two News rows remain scannable at 390px and at least four at desktop height
   without horizontal overflow.
10. At `1920px`, `1366px`, `834px`, and `390px`, verify `/macro` keeps all six
   module summaries, latest fact time, coverage, History Depth, and Data Quality
   readable without horizontal overflow or machine-only labels. Verify each
   module route has a real-sized module-specific chart, exact source clocks, an
   equivalent data table, and only its active hash section mounted. Select a
   non-default section, reload, and verify the same section remains active.
