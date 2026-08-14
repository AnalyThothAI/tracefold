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
  information archetypes with `data-page-archetype`: `scan` for Radar and News
  lists; `case` for Search, Token Case, and News Story; `decision` for
  Macro. The archetype governs hierarchy and density, never data ownership or
  business inference.
- **Data ownership.** Feature-owned API hooks, page hooks, and controller hooks own server reads/writes. Route modules and presentational UI components consume those feature hooks and must not call `useQuery`, `useMutation`, `useInfiniteQuery`, `getApi`, `postApi`, or `queryClient.set*` directly. `frontendDataOwnership.test.ts` enforces this boundary for `web/src/routes` and `web/src/features/*/ui`.
- **URL state.** Shareable Search and Token Case options live in their owning route-state helpers. Token Radar has no filter, window, venue, sort, selection, or pagination state. Its internal-navigation scroll position travels only in same-session router state and does not survive a hard reload.
- **Socket lifecycle.** `shared/socket` owns authentication, Token Case live-market cache patches, and ref-counted market-target subscriptions. The React client sends `replay: 0` and does not retain public `event` messages; backend event/replay remains a public evidence contract for other consumers. Token Radar registers no market target and is never patched from WebSocket state. Token Case subscribes only its active target. Stream/poll workers emit live market messages only after durable current-row persistence; those messages remain a cache enhancement, not a second source of truth. The Radar v5 hard cut changes no WebSocket route, message, replay, or subscription behavior.
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
  active target for live market updates. The dossier renders profile/market
  facts and raw posts without synthesizing prose or per-post conclusions. A
  Radar link carries `window=4h`, `focus=trigger`, and the canonical
  `trigger_event_id`; the route locates and visually focuses that exact Event
  or states that it is unavailable. It does not reconstruct a retired Radar
  rank, lane, decision, or score.
- **Token Radar drilldown.** Token Radar is the scan surface. Every whole Item
  is one internal Token Case link targeting its canonical identity and exact
  trigger Event. Copy remains a row-local button and supported on-chain
  identities keep a separate GMGN link in a new tab; neither activates the
  card link. Token Case stays in the current tab, shows an explicit Radar return
  action, and restores the bounded queue scroll from same-session route state.
  The browser keeps server order and never scores, filters, admits, fills, or
  reorders Items.
- **Token Radar currentness.** `/` is one full-height, maximum-fifty rich
  research queue over `token_radar_snapshot_v5`. The exact top level is
  `schema_version`, `social_evidence_as_of_ms`, `eligible_total`, and `items`;
  there is no Radar health state or state banner. A cached polling failure keeps
  the last successful queue silently. Only a first-read failure uses the route
  error surface, while a valid empty snapshot shows `No eligible cases`.
  Each real row renders the
  server-provided same-origin icon or a fixed-size symbol fallback, symbol/name,
  chain/address, current USD price, price change since the signal, market
  capitalization, and transparent attention/evidence. Identity plus current
  market facts form the primary visual layer; why-now and evidence form the
  secondary layer.
  Identity uses one primary symbol/name line plus one canonical identity line;
  canonical chain identifiers are rendered as human-readable network names and
  supported addresses expose an explicit GMGN destination. The header states
  that the queue is four-hour causal change, preserves newest-qualification
  order, and caps the public result at fifty. The exact trigger source-event
  time and qualification time are distinct semantic `<time>` values. Evidence
  labels the actual independent-author count rather than a prior-relative
  "new author" count. Price and market capitalization each render their own
  observation clock; a missing value/clock is labelled as unavailable evidence
  rather than rendered as an unexplained dash.
  Market and evidence are separate labelled scan groups rather than one clipped
  prose string. Desktop keeps at least four complete decisions visible at the
  `1280x504` sidebar boundary; the reported `1210x504` viewport remains fully
  contained, and mobile evidence values wrap instead of being ellipsized.
  It does not request `/api/recent`, subscribe to market targets, buffer
  WebSocket events, hydrate profiles or market data, pre-mount fifty empty rows,
  or render a Tape/task switcher. The header reports the displayed count against
  `eligible_total` plus the static `social_evidence_as_of_ms` timestamp; market
  clocks never advance that social label. The one feature
  query polls `/api/token-radar` every 30 seconds with an ETag-bound conditional
  GET; a `304` reuses the exact cached snapshot. Image elements may read only the
  same-origin paths already present in that snapshot.
  Cached content survives a recoverable transport refresh error without a
  delay/stale banner; the next scheduled poll recovers automatically. There is
  no green health badge, per-second age timer, client-side staleness inference,
  or window/venue frame cache.
- **News routes.** `/news` is a decision-first scan surface over the flat global
  Story Feed from `/api/news/feed`; the browser never clusters, scores, selects
  provider evidence, or reorders. The public News navigation contains `全球新闻`,
  `公共全球简报`, `状态`, and `来源`, backed respectively by `/news`,
  `/news/brief`, `/news/status`, and `/news/sources`. Story detail remains at
  `/news/stories/:storyId` and carries the same navigation. There is one public
  operator-bound product and no reader-personalized or user-adjustable score threshold.
  OpenNews admission is the exact configured account Strategy allowlist; the
  browser neither displays the private Strategy IDs nor reimplements their
  provider-owned rules. The default `全部` mode requests the complete current
  server population with `sort=latest` and `limit=25`; only URL-owned
  `view=focus` adds the fixed strict `provider_score_gt=70`. Tracefold importance
  remains an optional server sort. `/news/brief` renders one whole atomic
  current/LKG snapshot, never publication history or a personalized variant.
  Its server-ordered Top Stories are the primary document; L1 or degraded L2
  prose is a separately labelled enhancement and cannot reorder or replace that
  evidence.

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

  Feed rows are compact reading cards. Deterministic severity, actual reporting
  origin, exact local date/time plus relative time, and independent-origin count provide context. The
  canonical persisted headline is primary; the browser makes no model call.
  Only a valid safe plain-text description is shown, clamped to two lines.
  The backend-selected numeric OpenNews score is the first compact metadata
  badge and is omitted without numeric evidence. Non-empty
  `provider_metadata.assets` follow under the generic `关联资产` label in
  provider order; these labels may be crypto, equities, oil, or other
  commodities. The browser does not infer an asset class, correct provider
  labels, or hide CL-family labels from the reading surface. Missing assets are
  labelled `上游未标注` without hiding the Story. Provider signal is rendered
  as both text and a semantic card rail; grade remains omitted. Each row also
  renders the server-owned `notification` lifecycle: the durable delivery fact
  is primary and current ineligibility is secondary, so a historical `sent`
  Story never becomes visually `不通知`. The browser does not reimplement Push
  qualification. Tracefold importance is secondary and its supplied factors live
  under row-local `为什么重要`. The
  primary row action opens
  `/news/stories/:storyId`; a separate link opens original evidence when a
  valid URL exists and is otherwise omitted. Missing values are omitted.
  At 1280×720 the target is at least four to five rows, and at 390×844 about two
  rows, with no horizontal overflow.

  `/news/stories/:storyId` reads `/api/news/stories/{story_id}` and is
  reading-first: canonical headline, selected numeric OpenNews score and signal,
  provider-labelled `关联资产`, server-owned notification lifecycle, severity, reporting origin/time,
  independent-origin count, valid description, and representative original
  link precede related reports. Complete member
  pagination remains reachable. Internal IDs, complete factor math, provider
  metadata, and aggregation evidence do not replace canonical evidence.
  User-facing language is `新闻事件`, `相关报道`, and `独立来源`; machine terms
  remain inside audit disclosures. Linkless evidence remains valid; unavailable
  original-link actions are omitted from the reading layer.

  `/news/status` reads `/api/news/status` and presents the current OpenNews WSS
  state, connected/disconnected clocks, inbound and Story-visible P50/P95,
  configured/observed counts, last accepted trigger, official Strategy-history
  status, and the typed incident ledger
  before public RSS breadth/corroboration, followed by Story, Brief, Push, and
  translation evidence without creating a second health calculation. RSS
  displays `未启用` when the server reports its default-off configuration; zero
  enabled sources is not presented as warming or failure. The Feed
  also renders compact reader-facing News health in its header.
  That compact header alone requests `/api/news/status?view=realtime` every
  three seconds; it does not poll the complete Operations document or its
  24-hour Push diagnostics.
  `/news/sources` reads `/api/news/sources`, preserves its OpenNews-first server
  cursor order across enabled sources, exposes current Strategy/fetch outcome evidence, and never introduces
  client-side source ranking or enablement. The OpenNews inventory card says
  Strategy automatic push rather than displaying its reporting-origin tier as
  an acquisition priority. Reconnect changes current WSS state independently of
  historical incident recovery. Feed and its shallow realtime status poll every
  3 seconds; the Operations Status, Brief, and
  Sources poll every 60 seconds. Feed, Brief, and Status retain ETag
  revalidation and a `304` reuses the cached body.
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
  mirrored matrix/diagonal are presentation-only derivations.
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
- **CSS ownership.** `main.tsx` imports only Tailwind, tokens, and base styles. Feature and shared UI selectors are imported by the component or route that owns them. Shared primitives such as `IconButton`, `PageState`, `TokenProfileCard`, `CompactPanel`, and case-file components own their CSS under `shared/ui/`; feature CSS may lay out the containing toolbar or deck but must not redefine primitive internals. Do not use `.module.css` files as global selector buckets; CSS Modules must bind local classes from TypeScript.
- **CSS architecture harness.** `web/tests/architecture/cssArchitectureHarness.test.ts` is the future-proof gate for CSS ownership. It rejects retired global buckets (`cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, `signalLab.css`), side-effect CSS imported from non-local owners, feature CSS that redefines shared UI classes, feature selectors outside their namespace, naked modifier classes such as `.active` or `.gap`, and side-effect class names reused across feature roots. When a new feature needs side-effect CSS, add an explicit namespace policy there rather than borrowing another feature's selectors.
- **Cascade layers.** Side-effect CSS participates in the app cascade contract declared in `styles/tokens.css`: `app.base`, `app.primitives`, `app.shell`, `app.features`, then `app.overrides`. `styles/base.css` uses `app.base`; shared primitives use `app.primitives`; cockpit shell files use `app.shell`; feature route CSS uses `app.features`. Unlayered side-effect CSS is allowed only for Tailwind's import file.
- **Responsive CSS contract.** Mobile behavior is a tested architecture surface, not a best-effort visual tweak. Shell CSS owns `.cockpit-shell`, `.cockpit-main`, `.center-column`, `.topbar`, and the shadcn sidebar composition (`SidebarProvider`, `AppSidebar`, `SidebarInset`, and `SidebarTrigger`) split by owner files (`cockpitShell.css`, `CockpitTopbar.css`, `AppSidebar.css`, and `cockpitShellContract.css`). Final shell breakpoint decisions, including the mobile topbar row height token, live in `features/cockpit/ui/cockpitShellContract.css`. Mobile and tablet route navigation uses the shadcn `Sheet` drawer opened from the topbar trigger. `features/live/ui/live.css` owns a single full-height compact queue at every viewport; it has no responsive controls row or table-column mode.
- **Route controls.** Shells do not render route-specific filter controls. News, Macro, Search, and Token Case controls belong to the feature route that consumes them; Token Radar has none. `CockpitShell` and `SearchShell` own only navigation, frame layout, the main route scroll container, and route-appropriate hotkeys.
- **Shell navigation.** Desktop users navigate through the collapsible shadcn `AppSidebar`; tablet and mobile users open the same route tree through the topbar `SidebarTrigger` and shadcn drawer. The primary route tree contains exactly Radar, News, and Macro in that order; Search remains reachable through the topbar submit flow. Healthy runtime state is silent. Configuration, service, or realtime anomalies appear as an accessible topbar status; operational diagnosis remains on the API/CLI surfaces and there is no browser Ops route. Live has no route-local bottom task navigation. `/stocks` is removed and resolves through the standard not-found route; there is no redirect or compatibility screen.
- **Scrolling.** `body` remains locked for the app shell. `.center-column` is the shell-managed route scroll container. `RadarPage` owns one `minmax(0, 1fr)` Radar row at every viewport, and the compact queue scrolls inside `.live-radar-items`; no retired table, bottom deck, controls row, or mobile task-bar reserves height. Route-level nested scrollers are allowed only when they are intentionally bounded and covered by Playwright overflow/reachability assertions.
- **Breakpoint policy.** Desktop density starts at `1280px`. Tablet uses a single route column from `768px` through `1279px`. Mobile rules are `max-width: 767px` and must appear late enough in the cascade to win over base and desktop/tablet rules. Use container queries for local card/panel behavior when component width matters more than viewport width.
- **Side-effect CSS budget.** Architecture tests fail any side-effect CSS file above 500 lines. Component-specific styling should move toward CSS Modules or smaller owner files instead of growing route-wide side-effect CSS buckets.
- **Accessibility.** Icon-only controls use `IconButton` with an explicit `aria-label`; route status regions use polite live regions; form controls need visible or screen-reader labels. `jsx-a11y/recommended` is enforced as an error gate.
- **Score display.** Any displayed ranking score includes its component breakdown from the API. The UI does not recompute ranking facts locally.
- **Token images.** Token Case/profile surfaces render
  `profile.identity.logo_url` directly, and Radar renders the v5 Item
  `target.logo_url` directly. The API contract guarantees either value is
  `null` or a same-origin `/api/token-images/{image_id}` path; DB
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

1. Hard-reload `/`, `/search`, `/news`, `/news/brief`,
   `/news/status`, `/news/sources`, `/news/stories/:storyId`, `/macro`, and
   `/token/:targetType/:targetId?window=4h` with representative query
   params.
2. Submit the topbar search and confirm the URL becomes `/search?q=<submitted-query>`.
3. Verify visible loading/empty/error states are structured, labelled, and non-overlapping.
4. Confirm no failing `/api/*` requests in the browser session.
5. Confirm route-aware WebSocket subscription behavior: `/` registers no
   `market_targets`; Token Case registers only its active target and releases it
   after leaving the route.
6. Confirm Token Case/profile logos either load from `/api/token-images/{image_id}` or show
   fallback marks, with no browser requests to provider image URLs such as
   GMGN `external-res`.
7. At `390px`, confirm the topbar `SidebarTrigger` opens the shadcn drawer, drawer route links are reachable, `.topbar` and `.center-column` do not overlap, the full-height Radar shows its static social-evidence timestamp and independent Item clocks, no filter/Tape/task bar exists, each Case action is reachable, and the final Radar Item is visible without overlap.
8. At tablet width around `834px`, confirm the desktop sidebar is hidden, the topbar trigger opens the shadcn drawer, drawer route navigation and topbar search still work, and the Radar title, labelled market/evidence groups, full-height list, and no-overflow contract remain intact.
9. At `1920px`, `1366px`, `834px`, and `390px`, verify the default News Feed
   requests complete latest 25-row pages; explicit `重点` adds only the fixed
   strict `provider_score_gt=70`; search and origin filters
   survive mode changes and alter server results; canonical-headline rows remain
   readable; the selected OpenNews score and signal plus the server-owned Push
   lifecycle are visible while grade remains absent; exact date/time remains visible;
   current WSS state and latency are inline; and Story
   audit evidence starts collapsed. On
   `/news/brief`, verify Top Stories stay in exact server order, citation links
   open the matching Story, linkless evidence remains visible, L1/L2 is labelled
   separately, current/degraded/LKG/unavailable states are truthful, and no
   history or personalized controls appear. Verify `/news/status` shows the four
   server-owned layers and `/news/sources` preserves server order with reachable
   pagination. Confirm about two News rows remain scannable at 390px and at
   least four at desktop height without horizontal overflow.
10. At `1920px`, `1366px`, `834px`, and `390px`, verify `/macro` keeps all six
   module summaries, latest fact time, coverage, History Depth, and Data Quality
   readable without horizontal overflow or machine-only labels. Verify each
   module route has a real-sized module-specific chart, exact source clocks, an
   equivalent data table, and only its active hash section mounted. Select a
   non-default section, reload, and verify the same section remains active.
