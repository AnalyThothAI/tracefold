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
- **News route.** `/news` is a decision-first scan surface over the flat global
  Story Feed from `/api/news/feed`; the browser never clusters, scores, selects
  provider evidence, or reorders. Its three modes are `重点`, `全部`, and
  `中文简报`. The default `重点` request is the fixed current 12-hour population
  with `provider_score_gt=70`, `sort=latest`, and `limit=25`; the comparison is
  strict, so a score of 70 is excluded. `全部` removes only the score threshold.
  Tracefold importance remains an optional server sort. `/news/brief` is the
  stable shareable Brief URL and participates in the same mode navigation; its
  immutable publication history is collapsed by default.

  News query, mode, category, deterministic severity, actual reporting origin,
  and sort are URL-owned. Search calls the News Feed with `q`; it does not call
  `/api/search/inspect` or reuse token resolver state. The backend searches and
  filters before cursor pagination. Active filters are removable chips inside
  one compact filter disclosure. Pagination is an explicit `加载更多新闻` action,
  loaded pages deduplicate by Story ID, and there is no automatic infinite
  scroll or client-side time-window control. A refreshed first page inserts new
  Stories at the top when the reader is already there. When the reader is
  scrolled away, the route preserves the viewport and shows a bounded new-item
  affordance that returns to the top.

  Feed rows are compact reading cards. OpenNews score, localized signal, and
  deterministic severity lead; actual reporting origin, relative time, and
  independent-origin count provide context. A ready zh-CN title is primary
  only when its returned source-title binding matches the exact displayed
  original; that original remains a subdued one-line secondary title. Pending,
  failed, unavailable, or mismatched translation falls back immediately to the
  original and may show a quiet `暂无译文`. Only a valid safe plain-text
  description is shown, clamped to two lines. Coins are limited to three plus a
  `+N` remainder. Tracefold importance is secondary and its supplied factors
  live under row-local `为什么重要`. The primary row action opens
  `/news/stories/:storyId`; a separate link opens original evidence when a
  valid URL exists and is otherwise omitted. Push state
  and provider grade do not appear in Feed rows. Missing values are omitted.
  At 1280×720 the target is at least four to five rows, and at 390×844 about two
  rows, with no horizontal overflow.

  `/news/stories/:storyId` reads `/api/news/stories/{story_id}` and is
  reading-first: bound Chinese/original title, OpenNews score/signal, severity,
  reporting origin/time, independent-origin count, coins, valid description,
  and representative original link precede related reports. Complete member
  pagination remains reachable. Internal IDs, complete factor math, provider
  metadata, and aggregation evidence stay under `为什么重要` or
  `评分与审计证据` disclosures. User-facing language is `新闻事件`, `相关报道`, and
  `独立来源`; machine terms remain inside audit disclosures. Linkless evidence
  remains valid; unavailable original-link actions are omitted from the reading
  layer.

  `/news/status` is not a browser route. The Feed renders compact News health
  from `/api/news/status` in its header: healthy state is quiet, recovery/stall
  is reader-facing, and the disclosure surfaces ingest and Story-title
  translation evidence without transport jargon. Other operational layers
  remain available in the API/CLI and may be added to the disclosure only when
  they aid a reader decision. The standalone
  `/news/sources` browser route and navigation entry are deleted; the
  `/api/news/sources` operator contract remains available. Feed and Brief poll
  every 60 seconds with ETag revalidation; a `304` reuses the cached body.
  There is no archive, revision timeline, read state, favorites, subscriptions,
  per-Story AI panel, push inbox, notification settings, browser model call, or
  adjustable score threshold.
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

  Rates begins with the persisted 2Y/10Y/30Y completed-session matrix,
  2s10s/10s30s, and aligned 10Y/30Y nominal-real-breakeven decomposition. It
  then renders maturity cross-sections, source clocks, Fed institutional stance,
  officials distribution, and the event timeline. Cross-Asset keeps the fixed
  ETF matrix, normalized comparison, futures, and USD-index facts distinct.
  Credit keeps its four concurrent dimensions and no composite score.
  Volatility alone owns the official-expiry CFE VX settlement curve.

  The browser never calculates a Macro metric, merges source identities,
  chooses a fallback conclusion, invokes a model, or repairs persisted state.
  A missing module renders its typed unavailable reason without hiding the
  other five. At desktop, tablet, and mobile widths, content becomes labelled
  stacked sections without horizontal scrolling or hover-only evidence.
- **Page state.** Only an active first HTTP request may show Loading.
  Bootstrap pending/error, disabled query, transport error, same-session stale
  cache, and typed module-unavailable states use distinct `PageState.*`
  surfaces with a truthful retry/recovery action. A disabled token query must
  never leave an infinite skeleton.
- **CSS ownership.** `main.tsx` imports only Tailwind, tokens, and base styles. Feature and shared UI selectors are imported by the component or route that owns them. Shared primitives such as `IconButton`, `RadarControls`, `PageState`, `TokenProfileCard`, `DecisionTag`, `CompactPanel`, and case-file components own their CSS under `shared/ui/`; feature CSS may lay out the containing toolbar or deck but must not redefine primitive internals. Do not use `.module.css` files as global selector buckets; CSS Modules must bind local classes from TypeScript.
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

1. Hard-reload `/`, `/search`, `/stocks`, `/news`, `/news?view=all`,
   `/news/brief`, `/news/stories/:storyId`, `/macro`, and
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
9. At `1920px`, `1366px`, `834px`, and `390px`, verify the News focus mode
   requests strict `>70`, latest, 25-row pages; search and origin filters change
   server results; compact bilingual/fallback rows remain readable; Feed health
   is inline; Brief history and Story audit evidence start collapsed; and no
   `/news/sources` navigation survives. Confirm about two News rows remain
   scannable at 390px and at least four at desktop height without horizontal
   overflow.
10. At `1920px`, `1366px`, `834px`, and `390px`, verify `/macro` keeps all six
   module summaries, latest fact time, coverage, History Depth, and Data Quality
   readable without horizontal overflow or machine-only labels. Verify each
   module route has a real-sized module-specific chart, exact source clocks, an
   equivalent data table, and only its active hash section mounted. Select a
   non-default section, reload, and verify the same section remains active.
