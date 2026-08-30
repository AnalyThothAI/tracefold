# News taxonomy v1

`news_taxonomy_v1` is the versioned fact classification for ordinary
`event_kind=news`. It is persisted inside the existing atomic editorial
Judgment. It is not a second Event truth, a delivery score, or Trading input.

The production rollout for issue #117 intentionally precedes the model-quality
claim: new judgments carry this contract now, while quality remains `UNKNOWN`
until the preregistered Gold, future-holdout and rollout denominators are met.
Schema deployment is not evidence that the classifier passed those gates.

## Identities and ownership

- Taxonomy version: `news_taxonomy_v1`.
- IPTC Media Topics snapshot: `2026-01-05`, 35 reviewed qcodes.
- Codebook SHA: `6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac`.
- Source-authority classifier: `news_source_authority_v2`, registry SHA
  `bf092263462f93c58f58adfb7e6fa02037dbdd83326fdc516690501773b55af8`.
- Production Program: `news_semantic_program_v8`, Program SHA
  `404ad791ba68b0898f6fa07ad7e919b33cd5031a2bee27383f3a6030607aaefc`.
- Review contract: `news_review_v6`.
- The model emits `subject_codes`, `event_family`, `change_state`, and
  `assertion_status`. Code derives `source_authority` only from the structured
  reporting-source field. Strategy/provenance routing IDs carry no source
  authority. The exact allowlists live in `tracefold.news.taxonomy`; prose is
  not a second editable copy.

The upstream references are [IPTC Media Topics](https://iptc.org/standards/media-topics/),
[IPTC NewsML-G2 guidelines](https://www.iptc.org/std/NewsML-G2/guidelines/),
and [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
IPTC supplies stable subject identities; it does not own Tracefold's event
families, evidence status, policy, or delivery decisions.

## Exact contract

`subject_codes` is a canonical set of zero to three pinned qcodes. Unknown
codes, more than three codes, and selecting the broad
`medtop:04000000` parent with one of its pinned descendants fail closed.
Subjects answer “what domain is this about”; they do not replace the event
family. Empty is an honest abstention.

`event_family` answers “what happened”:

| Value | Plain-language boundary | Positive example | Exclude |
| --- | --- | --- | --- |
| `financial_results` | Realized accounting or operating results | quarterly revenue reported | guidance about a future quarter |
| `guidance_outlook` | Forward company outlook or guidance | company raises FY revenue range | already realized earnings |
| `product_service_change` | A product, protocol, service, or capability changes state | product becomes generally available | partnership recap or brand campaign |
| `corporate_transaction` | Ownership or corporate-structure transaction | merger, acquisition, spin-off, JV | ordinary commercial contract |
| `financing_capital_allocation` | Funding, debt/equity issuance, dividends, buybacks, or capex allocation | board authorizes a buyback | acquisition consideration itself |
| `leadership_governance` | Executive, board, governance, or control change | CFO resigns; directors elected | broad legal enforcement action |
| `regulatory_legal` | Rule, approval, filing action, lawsuit, or enforcement | regulator approves an application | a filing that merely contains earnings |
| `security_operational_incident` | Security breach, outage, exploit, or material operational failure | exchange halts after a breach | planned maintenance |
| `market_access` | Listing, delisting, venue, or eligibility access changes | ETF starts trading; token delisted | ordinary price or flow movement |
| `market_flow_price` | Material price, positioning, fund-flow, or market-liquidity fact | ETF inflow; abrupt price dislocation | structured OI/liquidation lanes |
| `macro_policy_data` | Economic data, central-bank policy, fiscal policy, trade policy | CPI reported; central bank cuts rates | company guidance |
| `geopolitical_conflict` | Conflict, sanctions, diplomatic or cross-border security development | ceasefire announced; sanctions imposed | domestic corporate regulation |
| `other` | Evidence is in scope but no supported family is defensible | bounded fact outside this codebook | noise, weak value, or a forced guess |

A filing is a source container, never automatically an event family. Preserve
SEC form, item, accession, CIK and XBRL facts as structured evidence, and label
the underlying financial, product, corporate, or regulatory event.

`change_state` is orthogonal to family:

- `announced`: an actor publicly announces a change; it is not yet in force.
- `scheduled`: a specific future time is fixed.
- `effective`: the change is live or legally in force.
- `reported`: a measurement or completed-period result is published.
- `updated`: a previously known fact receives a material detail without moving
  to another state.
- `delayed`, `cancelled`, `recalled`: the named lifecycle reversal occurred.
- `unknown`: evidence cannot support one state.

`assertion_status` describes evidence, not event type:

- `confirmed`: bounded evidence directly establishes the fact.
- `claimed`: one identified party asserts it without independent confirmation.
- `rumor`: the source presents it as unverified market talk or speculation.
- `conflicted`: material sources disagree.
- `unknown`: the bounded evidence does not support a stronger value.

`source_authority` is code-owned:

- `regulatory_filing`: exact recognized regulator/filing provenance.
- `issuer_first_party`: exact recognized issuer or venue first-party identity.
- `reputable_secondary`: exact recognized wire/publication identity.
- `unknown`: no exact allowlist match. Fuzzy names and fan accounts never
  inherit authority from a substring.

The classifier accepts only an exact normalized source name, an exact `@handle`,
or the exact hostname returned by the standard HTTP(S) URL parser. It never
splits arbitrary source text or consults strategy/provenance routing IDs. Values
such as `fan:reuters`, `fake|sec`,
`notreuters.com`, userinfo URLs, suffixes and unregistered subdomains therefore
remain `unknown`. The classifier version and registry address are part of the
Program execution envelope.

## Persistence and readers

Model-origin `EditorialEnvelope.v2` requires a complete taxonomy and hashes it
with TradeRelevance in the same `news_judgment_v2` atom. Ordinary readers accept
that current marker only. Earlier envelopes and judgments remain unchanged as
immutable database evidence, but they are archive-only: no Program, history,
review task, learning dataset, release gate, API, or Web path decodes or
translates them.

The Event detail API and console expose Chinese labels for all five axes, and
market-review discovery reads versioned `event_family`. ReaderHistory,
ToldContext, progression, learning replay, and evaluation carry full current
field names and exact current identities; none accepts a compact or historical
shape. Structured listing, OI, and liquidation presentation reads code-owned
`event_kind`; OI and liquidation use their own typed judgments and do not
fabricate model taxonomy or enter the generic Review v6 queue.

## Gold, shadow and evaluation

Only an accepted `news_review_v6` receipt is Gold. A model draft records its
author; that author cannot accept it. Product/financial/guidance families,
confirmed-vs-rumor, `other`/`unknown`, draft disagreement and other current-axis
critical cases require a different adjudicator before release eligibility.
Connected fact clusters are the independent sample; provider duplicates fold to
one representative.

`TaxonomyShadowProgramV2` is a content-addressed, one-Predictor offline program.
It uses the production-bounded evidence renderer and exact model identity, has
`release_authority=false`, and can append only shadow/evaluation artifacts to
the existing learning ledger. It cannot write an Event, verdict, card,
delivery, canary, promotion, or Trading record.

Each eligible case appends one terminal observation: `success`,
`schema_invalid`, `provider_failure`, or `budget_deadline_failure`. The stock
JSONAdapter may make one format-fallback call, so an observation contains an
ordered one-or-two-call attempt ledger. Every physical attempt carries its
request, invocation, terminal disposition and exact `RecordedLM` recording;
first-invalid/second-success is replayable rather than discarded. Shadow,
Evaluation and Release live in separate owner modules, with no forwarding
compatibility surface.

Before opening a future holdout, run `tracefold news learning
taxonomy-register`. The command constructs the current Shadow Program from the
operator-owned model configuration and computes its Program/model-binding
addresses; those identities are not operator input. The command then
derives the tested Git SHA from the content-addressed Workers deployment
receipt already stored in PostgreSQL and also binds its active bundle, runtime
manifest, image digest, candidate set, registration time, and runtime revision.
The command joins the durable runtime-manifest row and recomputes its address;
an unversioned image, non-Git runtime revision, mismatched deployment revision,
or receipt for a different active bundle fails closed. An operator cannot
declare an unrelated tested commit.
The workers connection takes the registration time from the PostgreSQL clock
and appends a content-addressed `candidate_registration` containing the exact
production Program/envelope/runtime/policy, taxonomy/codebook/Review/metric,
shadow Program and model-binding identities. The model-binding SHA is over the
shadow observation's exact `{model_identity, model_binding}` object.

Run `tracefold news learning taxonomy-shadow --file CONTEXTS --limit N --out
RECEIPTS` with that registration SHA and a bounded `cases` array of exact
`TriageContext` documents. Registration/deployment verification occurs before
and after model execution, while provider I/O occurs outside the PostgreSQL
transaction. The final transaction writes only content-addressed
`shadow_observation` learning artifacts parented by the registration.

Then run `tracefold news learning taxonomy-evaluate --file CASES --out REPORT`
over the frozen case document. The document references that registration SHA,
four PostgreSQL `release_evidence` references for the same current
candidate/dataset/metric, and one accepted Review v6 Gold plus one
`shadow_observation` artifact SHA per case. The workers connection re-reads and
validates every registration, acceptance, judgment, ordinary-News Event,
evidence version, event time, connected-fact cluster and replayable shadow
recording from PostgreSQL. The release report carries separate typed evidence
for production action, asset grounding, novelty, and trade relevance, including
each denominator and stable/candidate/candidate-only failure counts. The command
derives each outcome from those counts, rehashes the subdocument, and joins its
release evidence, evaluation report, candidate, and dataset; a generic report
PASS or file-declared PASS cannot stand in for a named gate. Case IDs, timing,
slices, readiness roles and other denominators are projected from durable rows
rather than trusted from the file. The verified Gold ledger root, shadow
artifact addresses and complete terminal/attempt population enter the
population/split roots. Missing or non-PASS regression evidence prevents an
overall PASS.

`TaxonomyEvaluationReportV1` records those identities, confusion matrices,
per-class and multilabel metrics, five-axis abstention risk-coverage,
language/source/audience/scope slices, reviewer agreement, adjudication rate,
absolute current quality gates, exact Stable/Candidate regression receipts and
every data-readiness denominator. An unknown split, missing/mismatched durable
candidate registration, forged Gold receipt, or holdout item at/before
registration is rejected. A missing or malformed Shadow observation, attempt,
or recording remains in the eligible population and makes the schema and
terminal gates plus overall result `UNKNOWN`; a fully observed invalid attempt
is counted rather than coerced to zero. Only complete development
and at least 24-hour post-registration future holdout evidence can produce PASS
or FAIL.

## Non-authority and rollback

Changing taxonomy alone must not change `decide()`, Gate, ReaderCard, Delivery,
or Trading. The common successful production route remains exactly two serial
physical model calls; taxonomy is part of the existing EventSemantics output,
not a third Predictor.

Migration `20260829_0328` trips open canaries and records the identity and prior
evidence disposition. Review v5 and older Program evidence remains append-only
audit history and cannot enter Review v6 denominators. Worker startup opens the
new bundle-owned epoch. Rollback restores the prior exact image/bundle; it never
deletes or rewrites taxonomy judgments, reviews, or receipts.
