# #664 evidence-context engineering receipt

Date: 2026-09-19. Repository: AnalyThothAI/tracefold.

Started on #665 / #663 commit `dffa74dfd79c2a70024224031ebd024ed8ef0f32`;
#665 subsequently merged as `3b0d7f5818deccfec6ed0f43c72d9af46f566593` with
follow-up tests `3048c73a8` and `8da6e8e03`. This change consumes the existing
execution provenance, supervision masks and native-state release path. It does
not add an evaluator or optimizer. There was no production database access,
real model call, paid campaign, real-channel delivery, merge or deployment.

## What is established

Editorial Admission now stores received provider business JSON without preview
truncation. Content hashes and actual availability preserve replay identity;
empty legacy material can fill later and conflicts cannot overwrite old bytes.
The selector records exact offsets and separate current/related refs, includes
long-tail conditions, isolates numbered facts and labels omissions. Background
reads include unsent editorial facts without extending the sent-only ledger.
Origin/fact deduplication happens before each channel cap and before the final
four full-text reads; the material is not a count of independent confirmation.

The real PG integration fixture exercises Admission → persisted payload →
preparation → audited native DSPy EventSemantics/Taxonomy/ReaderCard → verdict
execution → read-only detail. The three physical model invocations are scripted,
not quality evidence. It also exercises a settled replay with no further model
responses available and immutable detail after later material arrival. Other
fixtures cover late fills, conflict diagnostics, as-of cutoffs, actual-send
history bindings, unrelated pages, DNS pinning/private redirects, byte limits,
429 cooldown, cancellation and retained physical extraction permits.

Receipt history is frozen from the send's verdict and evidence version, with
canonical aliases resolved at the send attempt. A later alias, title or verdict
cannot rewrite it. Legacy unbound receipts remain visible with missing
provenance. Pure history and SQL exclude equal/future rows; the separate revision
CAS still observes later receipts and retains the bounded re-ask.

## Query receipt

The attached [raw EXPLAIN ANALYZE / BUFFERS and timings](issue-664-retrieval-plan.json)
were obtained on disposable PostgreSQL 18.6 through the production query, after
ANALYZE: 25,001 Items/Events, 90-second spacing over 26 days, one percent matching
titles and every Event tagged BTC as deliberately heavy same-asset noise. There
were 25 warmed samples, 32 returned lightweight candidates, p50 **406.10 ms** and
p95 **588.27 ms**. No model or network time is included. Concurrent local test
work and shared host load can affect these timings; this is not a production SLA.

The plan uses scans/hash joins and source/fact deduplication before channel
limits. The time index supports older, larger histories; this fixture lies
entirely inside the window, so sequential scans are reasonable. A title GiST
index was removed after the actual plan showed it did not help this deduplicated
query. LIMIT alone is not offered as proof of bounded database work. Re-test at
production-like row counts and distributions before enabling external reads.

[Reproduction script](issue-664-retrieval-reproduce.py) uses the existing guarded
PG clone factory. Run from the repository root with `PYTHONPATH=.` and
`TRACEFOLD_TEST_POSTGRES_DSN` pointing to a disposable test database. It creates
and drops its own migrated clone, emits the plan and regenerates the DB schema
inventory. No production DSN is embedded.

## Validation

The initial boundary/typed regressions failed before repair. The focused real PG
material, history and crash/replay suite passed 33 tests. The frontend passed
245 unit/component/route tests, 23 architecture tests, TypeScript, ESLint and a
production build. Full hermetic and PG results, installed distribution and
static/drift checks are recorded in the PR once completed; pending work is not
reported as passing.

## Quality experiment: NOT RUN / no promotion claim

No authorized real-model budget or accepted paired corpus was supplied. The
engineering results establish preservation and provenance, not better editorial
quality. KEEP the current model routes and policy thresholds; do not register or
promote a GEPA candidate from these fixtures. Optional HTTP remains disabled by
default. Engineering merge, quality improvement and production activation need
separate receipts; this PR does not close #651 or #663 automatically.

For a later authorized campaign, freeze the #663 accepted corpus and independent
split before looking at results. Compare A (archived actual input), B (current
source spans), C (B plus background/fixed history) and D (C plus bounded HTTP),
with the same model route and instructions wherever the input contract allows.
Missing historical full text cannot be fetched today and relabelled historical;
report such cases separately as today's counterfactual input study. Keep
history-distinct cases, group source copies into the same split and include
unsent/failed/filtered Events. Only after the input comparison consider demos or
GEPA through #663's existing objective and publication machinery.

Before execution, record per-arm budget and latency ceiling, material/query/
selector/model/prompt/metric identities, denominators, and missingness. Primary
coverage measures are necessary material in storage, candidate recall, selected
coverage and joint coverage of all required conditions. Separately report typed
subject errors, state overstatement, lost conditions, background contamination,
unsupported assertions, missed real progress and duplicate reminders, with paired
differences and uncertainty. Report real-distribution and hard-counterexample
sets separately. Require all deterministic boundary tests to pass; reject any
observed temporal leakage or unsupported-source promotion. Quality KEEP/NO_GAIN
is valid; choosing a numeric quality threshold after seeing results is not.

Retrieval timings above are the only measured stage timings. Production material
coverage, prepare/LLM/delivery p50/p95, tokens, paid cost and editorial error rates
are **not measured**. HTTP fixtures establish per-call physical limits, not live
provider reliability. The term-based relationship selector is an initial
engineering heuristic and does not prove a retrieved page refers to the same
state of an event; exclusions and attributed source text remain inspectable.
