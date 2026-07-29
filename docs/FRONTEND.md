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
  research-workbench language. `styles/tokens.css` is the only semantic color,
  type, radius, focus, and shell token contract; production code must not add a
  parallel theme or compatibility alias. Stable routes declare one of three
  information archetypes with `data-page-archetype`: `scan` for Radar, Stocks,
  and News lists; `case` for Search, Token Case, and News Story; `decision` for
  Macro. The archetype governs hierarchy and density, never data ownership or
  business inference.
- **Data ownership.** Feature-owned API hooks, page hooks, and controller hooks own server reads/writes. Route modules and presentational UI components consume those feature hooks and must not call `useQuery`, `useMutation`, `useInfiniteQuery`, `getApi`, `postApi`, or `queryClient.set*` directly. `frontendDataOwnership.test.ts` enforces this boundary for `web/src/routes` and `web/src/features/*/ui`.
- **URL state.** Shareable filters such as `window`, venue, search query, selected target, and radar sort live in route-state helpers. Local stores are only for interaction state that should not survive hard reloads.
- **Socket lifecycle.** `shared/socket` owns authentication, live-market cache patches, and ref-counted market-target subscriptions. The React client sends `replay: 0` and does not retain public `event` messages; backend event/replay remains a public evidence contract for other consumers. Routes register only the market targets they currently need; leaving Token Radar releases its market targets. Stream/poll workers emit live market messages only after durable current-row persistence; those messages patch visible market response keys but are not a second source of truth.
- **Search route.** `/search` reuses the cockpit topbar but owns its
  search-local rail, filters, resolver candidates, and selected result. Topbar
  submit navigates to `/search?q=<query>`. Token search results render the
  shared Token Case panel directly from `/api/search/inspect`; they do not
  fetch `/api/token-case` again. Token, topic, and ambiguous results render
  resolver, identity, source-post, current Radar, and market facts only.
- **Token Case route.** `features/token-case` owns persistent
  `/token/:targetType/:targetId` inspection. The route parses `window` and
  timeline sort from the URL, fetches `/api/token-case`, seeds
  `/api/target-posts` from the dossier's first page, and subscribes only the
  active target for live market updates. The dossier renders current
  factor/market metadata and raw posts without synthesizing prose or per-post
  conclusions.
- **Token Radar drilldown.** Token Radar is the scan surface. Primary row
  clicks route to
  `/search?q=<token-or-address>&window=<current>` for resolver
  context, while explicit token links may route to the Token Case dossier when
  a canonical target id is already known. Radar rows render the transparent
  factor snapshot supplied by the API; frontend code never recomputes ranking
  or introduces an additional admission state.
- **Token Radar currentness.** `/` is one full-height Radar and does not request
  `/api/recent`, buffer WebSocket events, or render a Tape/task switcher. The
  Radar header keeps title, count, and status contiguous on the left, with
  venue/window controls on the right at wide widths; narrow widths move
  only the controls to a second row. Its page-local status binds the exact
  current query identity. Content age is the browser clock minus
  `projection.source_max_received_at_ms`, clamped at zero and reformatted once
  per second without refetching. A true matching HTTP completion owns refresh
  health; React Query cache update time does not, because live-market messages
  can patch that cache. Old content alone never creates a warning. Cached rows
  survive a recoverable refresh/projection delay, while initial failure or more
  than 30 seconds without a true current-view HTTP success is unavailable.
  Only health transitions are politely announced; the advancing age is not an
  aria-live stream.
- **News route.** `/news` renders the flat global Story Feed from
  `/api/news/feed`; the browser never clusters, scores, or reorders. Category
  selection is a route-local filter, pagination uses the server cursor, and
  loaded pages deduplicate by Story ID. Every Story row shows level,
  representative source/time, NewsItem count, distinct physical-source count,
  importance, and its factor breakdown. `/news/stories/:storyId` reads
  `/api/news/stories/{story_id}` and shows the complete evidence membership,
  representative item, scoring item, and current/archived state—there is no
  revision timeline or per-Story AI panel. `/news/brief` renders the single
  Chinese World Brief, selected Story evidence, truthful publication/run
  state, and immutable publication history from `/api/news/brief`.
  `/news/sources` renders membership, fetch outcome, direct/relay path,
  latency, failure/backoff, and acquisition gate counts from
  `/api/news/sources`. Feed sorting is URL-owned:
  `sort=latest` selects publication time while the absent/default value uses
  importance; neither path reorders in the browser. Feed and Brief poll every
  60 seconds with ETag revalidation; a `304` reuses the cached body.
  Topbar search remains route-local and must not call `/api/search/inspect` or
  reuse token resolver state.
- **Macro routes.** `/macro` and `/macro/overview` are the daily decision
  overview. `/macro/rates-fed`, `/macro/economy-inflation`,
  `/macro/liquidity-funding`, `/macro/credit`, `/macro/volatility`, and
  `/macro/cross-asset` are the six typed decision modules backed by matching
  `/api/macro/*` routes. They do not accept a generic window parameter.
  `/macro/research` is the history/detail view of the same immutable Thesis
  product backed by `/api/macro/research`; its optional
  `session_date=YYYY-MM-DD` survives hard reload and sharing. It is not a
  second Macro narrative.

  The overview is a 30-second decision brief with one fixed order: current
  session/publication state, call/no-call mainline, material changes, one to
  three causal edges, strongest support/counterevidence, real tensions, sparse
  material outlook plus all twelve asset facts/recovery, catalysts/conditions,
  scoped Live Delta/Outcome Replay, then compact provenance/data-quality
  audit. It does not render six equal narratives or 12×2 model filler.

  The intended 08:50 session and deterministic cutoff remain explicit. When the
  requested current session has no publication, the UI shows its exact
  `pending`, `running`, `retryable`, `failed`, `config_error`,
  `not_published`, or `missing` state and never injects an older Thesis.
  Historical v1/v2 publications are available only after an explicit dated
  research selection. Current/stale transport state is separate from Thesis
  state, Coverage, Current Health, History Depth, and backfill execution.

  Each module is a typed evidence workbench. A thin shared header answers
  current conclusion, latest change, as-of, data health, and relationship to the
  Thesis; the active hash-selected section then renders module-specific facts,
  charts, why they matter, linked claims, real counterevidence, and real next
  checkpoints. Empty semantic sections are omitted. There is no shared
  contradictions/falsifiers/checkpoints/gaps four-card footer and no fixed
  business interpretation. Release modules distinguish expected, actual,
  surprise, revision, source publication time, and receipt time. Optional
  required history can lower only the declared feature's History Depth, never
  Current Health. Optional maximum history stays in the audit disclosure.

  Cross-Asset defaults to the fixed ten-ETF matrix followed by a normalized
  comparison and best-effort major-futures/USD-index rows. ETF and futures rows
  distinguish five-year daily history from intraday price datasets and display
  the actual market timestamp rather than the HTTP receipt time. Dataset
  details show data, market, and source state independently plus group health;
  closed and maintenance sessions are not painted stale merely because wall
  time advanced. Rates renders true maturity cross-sections and a detailed tenor
  table. Fed renders institutional stance, recent officials distribution, then
  the event timeline. Credit keeps four concurrent dimensions visible above
  its ladder/funding/banks/quality/confirmation sections and never renders a
  composite score. Volatility owns the official-expiry CFE VX settlement curve;
  Cross-Asset does not duplicate it.

  The research page renders the same v2 report for the current session and an
  explicit immutable v1/v2 archive envelope for a dated session. It provides a
  real link back to the overview and keeps publication history separate from
  run status. Old Reviewer fields are labelled audit-only and never presented
  as current approval. The browser does not classify evidence sufficiency,
  infer direction/confidence, score or group assets, merge source identities,
  aggregate Live Delta, select fallback content, or recompute conclusions.

  Current business states and archive `historical/missing` remain visually
  distinct. Every non-success state displays its typed
  reason, affected scope, retryability, recovery action, and a next check only
  when one is actually scheduled. Persisted pending/running states may poll the
  read but never start or resume an Agent. Run status and sanitized errors are
  supporting metadata, not Thesis content.

  At desktop, tablet, and mobile widths the document becomes labelled stacked
  content without horizontal page scrolling or hover-only material evidence.
- **Page state.** Only an active first HTTP request may show Loading.
  Bootstrap pending/error, disabled query, transport error, same-session stale
  cache, and Thesis business states use distinct `PageState.*` surfaces with a
  truthful retry/recovery action. A disabled token query must never leave an
  infinite skeleton.
- **CSS ownership.** `main.tsx` imports only Tailwind, tokens, and base styles. Feature and shared UI selectors are imported by the component or route that owns them. Shared primitives such as `IconButton`, `RadarControls`, `PageState`, `TokenProfileCard`, `DecisionTag`, `CompactPanel`, and the research case-file components own their CSS under `shared/ui/`; feature CSS may lay out the containing toolbar or deck but must not redefine primitive internals. Do not use `.module.css` files as global selector buckets; CSS Modules must bind local classes from TypeScript.
- **CSS architecture harness.** `web/tests/architecture/cssArchitectureHarness.test.ts` is the future-proof gate for CSS ownership. It rejects retired global buckets (`cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, `signalLab.css`), side-effect CSS imported from non-local owners, feature CSS that redefines shared UI classes, feature selectors outside their namespace, naked modifier classes such as `.active` or `.gap`, and side-effect class names reused across feature roots. When a new feature needs side-effect CSS, add an explicit namespace policy there rather than borrowing another feature's selectors.
- **Cascade layers.** Side-effect CSS participates in the app cascade contract declared in `styles/tokens.css`: `app.base`, `app.primitives`, `app.shell`, `app.features`, then `app.overrides`. `styles/base.css` uses `app.base`; shared primitives use `app.primitives`; cockpit shell files use `app.shell`; feature route CSS uses `app.features`. Unlayered side-effect CSS is allowed only for Tailwind's import file.
- **Responsive CSS contract.** Mobile behavior is a tested architecture surface, not a best-effort visual tweak. Shell CSS owns `.cockpit-shell`, `.cockpit-main`, `.center-column`, `.topbar`, and the shadcn sidebar composition (`SidebarProvider`, `AppSidebar`, `SidebarInset`, and `SidebarTrigger`) split by owner files (`cockpitShell.css`, `CockpitTopbar.css`, `AppSidebar.css`, and `cockpitShellContract.css`). Final shell breakpoint decisions, including the mobile topbar row height token, live in `features/cockpit/ui/cockpitShellContract.css`. Mobile and tablet route navigation uses the shadcn `Sheet` drawer opened from the topbar trigger. `features/live/ui/live.css` owns a single full-height Radar at every viewport; its explicit status stays beside the title/count, while route controls wrap to a second row only at narrow widths without creating page overflow.
- **Route controls.** Shells do not render route-specific filter controls. Window and venue controls belong to the feature route that consumes them; `CockpitShell` and `SearchShell` own only navigation, frame layout, the main route scroll container, and hotkeys. Top-level radar routes must use owner-prefixed table selectors (`token-radar-*`, `stock-radar-*`) rather than generic historical selectors such as `.radar-row`, `.metric`, or `.phase`.
- **Shell navigation.** Desktop users navigate through the collapsible shadcn `AppSidebar`; tablet and mobile users open the same route tree through the topbar `SidebarTrigger` and shadcn drawer. The primary route tree contains exactly Radar, Stocks, News, and Macro in that order; Search remains reachable through the topbar submit flow. Healthy runtime state is silent. Configuration, service, or realtime anomalies appear as an accessible topbar status; operational diagnosis remains on the API/CLI surfaces and there is no browser Ops route. Live has no route-local bottom task navigation.
- **Scrolling.** `body` remains locked for the app shell. `.center-column` is the shell-managed route scroll container. `LivePage` owns one `minmax(0, 1fr)` Radar row at every viewport, and Radar rows scroll inside `.token-radar-table`; no retired bottom deck or mobile task-bar row reserves height. Route-level nested scrollers are allowed only when they are intentionally bounded and covered by Playwright overflow/reachability assertions.
- **Breakpoint policy.** Desktop density starts at `1280px`. Tablet uses a single route column from `768px` through `1279px`. Mobile rules are `max-width: 767px` and must appear late enough in the cascade to win over base and desktop/tablet rules. Use container queries for local card/panel behavior when component width matters more than viewport width.
- **Side-effect CSS budget.** Architecture tests fail any side-effect CSS file above 500 lines. Component-specific styling should move toward CSS Modules or smaller owner files instead of growing route-wide side-effect CSS buckets.
- **Accessibility.** Icon-only controls use `IconButton` with an explicit `aria-label`; route status regions use polite live regions; form controls need visible or screen-reader labels. `jsx-a11y/recommended` is enforced as an error gate.
- **Score display.** Any displayed ranking score includes its component breakdown from the API. The UI does not recompute ranking facts locally.
- **Token images.** Token profile and radar logos render
  `profile.identity.logo_url` directly. The API contract guarantees that value
  is either `null` or a same-origin `/api/token-images/{image_id}` path; DB
  constraints reject remote provider URLs before they reach the frontend. Do
  not restore `tokenImageUrl`, `/api/token-image?url=...`, local logo filters,
  or any frontend proxy/helper that rewrites GMGN, Binance, OKX, or CEX image
  URLs.

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

1. Hard-reload `/`, `/search`, `/stocks`, `/news`,
   `/news/stories/:storyId`, `/macro`, and
   `/token/:targetType/:targetId?window=1h` with representative query
   params.
2. Submit the topbar search and confirm the URL becomes `/search?q=<submitted-query>`.
3. Verify visible loading/empty/error states are structured, labelled, and non-overlapping.
4. Confirm no failing `/api/*` requests in the browser session.
5. Confirm route-aware WebSocket subscription behavior: token-radar
   `market_targets` are released after leaving `/`.
6. Confirm token logos either load from `/api/token-images/{image_id}` or show
   fallback marks, with no browser requests to provider image URLs such as
   GMGN `external-res`.
7. At `390px`, confirm the topbar `SidebarTrigger` opens the shadcn drawer, drawer route links are reachable, `.topbar` and `.center-column` do not overlap, topbar controls stay contained, the full-height Radar shows explicit content age and refresh health, no Tape/task bar exists, and the final Radar row is reachable without overlap.
8. At tablet width around `834px`, confirm the desktop sidebar is hidden, the topbar trigger opens the shadcn drawer, drawer route navigation and topbar search still work, and the Radar compact title/status group, wrapped controls, full-height list, and no-overflow contract remain intact.
9. At `1920px`, `1366px`, `834px`, and `390px`, verify `/macro` keeps the
   mainline, strongest evidence/tensions, changes, all twelve server-grouped
   assets, scoped Live Delta, Evidence Health, and Data Quality readable
   without horizontal overflow or machine-only labels. Verify each module has
   a real-sized module-specific chart, no fixed empty four-card footer, exact
   source clocks, an equivalent data table, and only its active hash section
   mounted. Select a non-default section, reload, and verify the same section
   remains active. On `/macro/research`, verify the claim-first chain, readable
   citations, requested/displayed session boundary, real Overview link,
   keyboard-reachable audit appendix, and historical session reload.
