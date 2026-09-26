# News EventUpdate cut (#706, PR #711)

The editorial News path now produces an adopted, versioned `EventUpdate`. It
describes claims, changes from prior claims, cited evidence, support or
refutation, and unresolved questions. Notifications and Trading consume that
result independently. Typed market facts (OI, liquidation, smart money and
wallet) keep their separate path.

## Running path

```text
OpenNews -> RabbitMQ raw -> admission -> Item / Event / evidence revision
                                         -> durable semantic work -> news.triage
NewsAgent: frozen input -> extraction checkpoint -> judgments -> observation
          -> head CAS + adopted EventUpdate + public outbox + notification work
                              |                          |
                              v                          v
                  App relay -> Trading          planner -> selected intent
                  catalyst / amendment          -> on-demand card -> sender
                                                 -> actual receipt and body
```

Admission persists a changed source body or attribution as an item evidence
revision, preserving that revision's source and receipt time, and wakes semantic
work in the same transaction. Exact retransmission is idempotent. A near match
joins an Event and supplies candidates; similarity does not decide equivalence or
end semantic work. The existing worker queue runs `SemanticWorker`, which claims
a lease, bounds attempts, and leaves failures visible instead of recording a
negative editorial verdict. Janitor re-wakes stale durable work.

Each turn sends only the new member bodies, body revisions or optional read not
yet analyzed for this Event. Its adopted claims and up to eight related Events'
claims remain comparison context; unaffected claims and their source links are
carried forward by assembly. Completed work records the analyzed evidence refs
even when the material yields no new claim, so a later member does not trigger
another extraction of that old material. Several arrivals while a worker is
busy are processed together at the latest wanted revision.

`NewsAgent` saves insert-only extraction and understanding checkpoints and an
observation, then adopts with a compare-and-swap on the Event head. Adoption,
public outbox and notification work commit together in a short transaction.
An older observation cannot move the head backward. Model, provider and broker
calls stay outside transactions. The optional extra read uses only existing
stored material and one durable reservation per lineage.

## Semantic and judgment contracts

One EventUpdate can contain several claims with separate mode, phase, timing,
assets and citations. Claims retain stable Event-local refs across revisions;
changes name prior refs and distinguish real new actions from correction,
conflict or evidence changes. Unknown comparisons become `possible_new`, not a
fabricated catalyst. Source relations and attributions are retained separately
from a claim's quoted spans. A publisher's authority describes the cited source;
it does not verify an allegation or future outcome.

The default path uses configured generative News endpoints. An optional,
News-specific `llm.news_judgment` route calls Jev through the existing System One
SDK and DSPy Choice/Noul adapter. Successful native answers are reused without
a second-model vote; a failed batch can use the matching generated judgment.
The cache keys the task, input and model identity. Generative extraction,
judgment and card routes have independent identities and code-owned budgets.
The semantic stage is bounded at 120 seconds, notification planning at 60
seconds, and an individual generative call at 60 seconds. Each native batch is
bounded by the lesser of two seconds and the remaining stage time.

## Reader notification

The planner decides each adopted claim by named reason. It handles commentary,
promotion, forecast, schedule-only material, unsupported price reports, stale
sources, watchlist matches, and actual previously sent coverage. Unknown mode
gets one bounded clarification; failure is `mode_unknown`. Only a full match to
the actual sent body suppresses a claim. An in-flight or ambiguous send blocks
overlapping claims without pretending the reader received them. `key` (⚡)
requires a state change or official measure in a key topic family with sufficient
source corroboration; it changes presentation, not fact authority.

The selected claim refs and adopted update define a stable `intent_id`.
CardComposer runs only for that selection and freezes its Chinese body. The
sender rechecks the head and reader revision before sending, stores the exact
body, digest, provider message ID and outcome, and retries a proved `not_sent`
with the same intent. An unknown outcome remains `ambiguous` and is not blindly
resent. Card or send failure does not retract the adopted update or public
outbox. Historical `first` and `followup` receipts retain their exact payloads
under deterministic legacy intent IDs; unsent legacy work is retired.

## Trading and read side

`public_updates` emits a structured `catalyst_delta` for an actionable new
claim and `source_update` for a correction, refutation or evidence amendment of
an earlier publication. It also supplies deterministic, cited source text; no
reader card is needed. App dispatches `source_update` before target selection.
Trading records it idempotently in `trading_source_amendments`, without creating
a Trigger or Case, extending freshness, cancelling an order, or adding trade
authority. A catalyst uses `news_public_update_v1`, chooses its target from the
changed claims' primary assets and starts freshness at first availability.
Historical headline/why payloads are rejected on the new path. A correction of
a cited proposition can reject a still-unsubmitted entry as `source_corrected`.

Serve exposes adopted update revisions, source relations, processing state,
claim-level plan reasons and exact delivery outcomes. Legacy verdicts remain
historical records under `legacy_verdict`; they are not converted into claims.
The React console renders the current update and processing state and no longer
filters on the retired four-axis taxonomy.

## Cutover and limits

Migration `20260926_0404` creates semantic/update/notification tables, changes
delivery keys to `intent_id`, adds item revisions and Trading amendments, and
widens the News trade-event kind. It is forward-only. Stop Serve and Workers,
back up the database, apply the migration, then start the matching image.
Existing operator config must remove `llm.news_compiler_reflection` and
`news.policy`; unknown keys fail validation. See [Migrations](../MIGRATIONS.md)
and [Setup](../SETUP.md) for the operational sequence.

The old three-Predictor Program, taxonomy axes, GEPA/learning/release/canary
plane, progression review and their CLI entry points are removed from current
execution. Historical database rows remain for audit. The retained ReviewDesk
reviews new intents; card judge calibration remains. This cut does not prove
model accuracy, notification volume, lower cost, or trading performance. The
production-window replay in #706 is diagnostic evidence, not a merge gate or a
live rollout. Deployment and trading execution require their own authorization.
