# News taxonomy v1

`news_taxonomy_v1` is the versioned fact classification for ordinary
`event_kind=news`. It is persisted inside the existing atomic editorial
Judgment. It is not a second Event truth, a delivery score, or Trading input.

The production rollout for issue #117 intentionally precedes the model-quality
claim. Issue #437 measures that claim from one frozen development Dataset of
accepted Gold and the taxonomy already persisted by Stable. Until at least 60
independent connected-fact clusters are scored, the result is
`INSUFFICIENT_DATA`; schema deployment is not evidence of model quality.

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
that current marker only. Migration `0336` physically deletes earlier envelopes
and judgments; no Program, history, review task, learning dataset, release gate,
API, or Web path decodes or translates their retired shapes.

The Event detail API and console expose Chinese labels for all five axes, and
market-review discovery reads versioned `event_family`. ReaderHistory,
ToldContext, progression, learning replay, and evaluation carry full current
field names and exact current identities; none accepts a compact or historical
shape. Structured listing, OI, and liquidation presentation reads code-owned
`event_kind`; OI and liquidation use their own typed judgments and do not
fabricate model taxonomy or enter the generic Review v6 queue.

## Gold and recorded Stable measurement

One operator-accepted `news_review_v6` taxonomy is Gold. There is no
taxonomy-specific second-reviewer or adjudication prerequisite. A model draft
still records its author and cannot accept itself, and a non-dry-run
`review accept-drafts` requires a non-empty `--only` list naming the Event or
task prefixes the operator actually approved. An empty selection is preview-only.

Freezing the existing development Dataset projects the accepted taxonomy into
the existing episode beside the production judgment. That projection is part
of `episode_projection_root_sha256`, so changing Gold changes the root. No
taxonomy table, Dataset kind, migration, or parallel corpus exists. Connected
fact clusters are the independent sample and provider duplicates contribute one
deterministically elected representative.

The operator records `primary_target: ...` on issue #437 before inspecting the
first production score, reviews and freezes 60–100 independent clusters, then
runs:

```bash
uv run tracefold news learning baseline --dataset DATASET_SHA \
  --mode recorded --out /tmp/taxonomy-baseline.json
```

This branch of the existing baseline command makes zero provider calls and
scores the taxonomy already persisted by the Dataset's recorded Stable cohort.
Its content-addressed `tracefold.news.recorded_taxonomy_baseline.v1` report
contains only Dataset, Stable Program, recorded runtime/model, and metric
identities; `case_n`, `independent_cluster_n`, and actual `scored_case_n`; the
metrics; and `MEASURED` or `INSUFFICIENT_DATA`. `MEASURED` means the fixed
minimum of 60 independent clusters was reached; it does not mean a target was
met. Accepted external misses remain visible in `case_n` and the Dataset root;
because they have no recorded Stable prediction, they are excluded from the
independent/scored population and all metric denominators.

The sole primary metric is `event_family` macro-F1 over legal labels with Gold
support greater than zero. Diagnostics are `subject_codes` micro-F1,
`change_state` accuracy, `assertion_status` macro-F1 over supported Gold labels,
and exact match across the four model-owned axes. Every metric publishes the
complete legal label universe, including support-zero classes, and all use the
same cluster-deduplicated population. Model non-abstain excludes
`source_authority`. That fifth, code-owned axis is reported separately as exact
deterministic registry coverage and never enters a model score.

There is no taxonomy registration, shadow, or separate evaluation lifecycle.
The former commands, Predictor, verifiers, storage reads, tests, and docs were
deleted in the #437 hard cut; the existing Dataset and recorded baseline are
the only measurement path.

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
