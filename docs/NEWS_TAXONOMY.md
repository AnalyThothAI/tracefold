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
- Production Program: `news_semantic_program_v9`, Program SHA
  `4fd8b3ef66ecac8caa6644acb1b13c1eb661480e7a39f876c3891194268f917e`.
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

The three label axes have one codebook, and it is code (#501 D3):
`EVENT_FAMILY_DEFINITIONS`, `CHANGE_STATE_DEFINITIONS`,
`ASSERTION_STATUS_DEFINITIONS` and `TAXONOMY_PRECEDENCE_RULES` in
`tracefold/news/taxonomy.py`. `render_taxonomy_seed_instruction()` renders the
taxonomy Predictor's seed text from those constants byte for byte, the GEPA
metric quotes the same definitions and precedence rules in its feedback, and
the blind Gold drafters run the same Signature and seed. This document does not
restate the definitions: a second prose copy is a second editable truth, and
the 2026-09-02 post-mortem of #456 found reviewer batches diverging on
`announced`/`effective`/`reported` and `confirmed`/`claimed` precisely where the
seed and the reviewer prose had drifted apart. Repairing a confused boundary
means editing the constant, which moves the seed, the Program identity and the
metric feedback together.

`event_family` answers "what happened"; a filing is a source container, never
automatically an event family. `change_state` is orthogonal to family.
`assertion_status` describes evidence, not event type. The label sets travel in
the typed `ModelTaxonomyV1` schema, which the JSON adapter hands the provider
as a grammar; the seed text carries only what a schema cannot: definitions,
precedence rules, the qcode glossary and the boundary examples.

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

## Gold and GEPA measurement

One explicitly accepted `news_review_v6` taxonomy is Gold. Gold is an
acceptance state, not a claim that an independent human supplied the label. An
owner-authorized AI adjudicator may accept an explicitly reviewed subset and is
recorded as AI, never as human.

Gold is drafted blind, twice (#501 D8). `news learning draft-reviews
--rubric-model M --taxonomy-models A,B` runs two drafters of different families,
neither the Stable task model, each over the Program's own bounded taxonomy
input — evidence and Gate facts, no card, no Stable label, no told ledger, no
review — through the taxonomy Predictor's Signature and seed. Agreement is the
draft; on disagreement the draft takes A, the batch marks
`taxonomy_disagreement`, and the accepting reviewer decides through the
existing `accept-drafts` edit. The accepted review's `taxonomy_review.drafts`
keeps both labels under their model names, and a development freeze reports
Cohen's κ over every dual-labelled cluster beside the corpus; κ is reported,
never a gate. The #456 post-mortem is the reason: the rubric drafter labelled
taxonomy while reading Stable's own label in `card_json`, so Stable-drafted
batches agreed with Stable at 0.95–1.00 and Codex-drafted batches at 0.03–0.17,
and the metric measured who drafted the label.

The existing development Dataset projects four model-owned axes into each
episode: `subject_codes`, `event_family`, `change_state`, and
`assertion_status`, plus the review's `taxonomy_review` provenance verbatim.
`source_authority` is derived from evidence by code and is not model Gold. No
taxonomy table, Dataset kind, migration or parallel corpus exists.

`taxonomy_metric.py` is a pure comparison helper. Per case it computes
subject-code set F1 (both empty is 1; exactly one empty is 0) and exact matches
for the other three axes, then averages the four values. The score and feedback
come from that one comparison; feedback quotes the codebook definition of the
expected and predicted label and any precedence rule written for that
confusion, never source authority.

Every case with valid accepted Gold and a replayable Stable answer is an
optimizer sample (#501 D9); the plan calls it `included`, records whether
Stable already matched (`stable_exact`), and reports `stable_exact_n` /
`stable_mismatch_n` as readiness diagnostics. Owner columns and `taxonomy_*`
dimension labels are audit metadata and grant no optimization authority. The
GEPA student is the single `taxonomy` Predict; the admitted candidate is GEPA's
own `best_idx` when its selection score is strictly above the seed's, otherwise
the run is `NO_OP`.

The public chain is the existing `news learning readiness` followed by one
`news learning run`; Dataset forms of `baseline` and standalone `optimize` do
not exist. The Candidate still passes the existing evaluator and release path.

## Non-authority and rollback

The four model-owned axes (`subject_codes`, `event_family`, `change_state`,
`assertion_status`) never enter `decide()`, Gate, ReaderCard, Delivery, or
Trading, and changing them alone must not change any of those. The code-owned
`source_authority` field is different: since policy v12 (#504) `decide()`
reads `editorial.taxonomy.source_authority` once, as issued from the evidence,
as the escalate corroboration fact — an eligible `escalate` from an `unknown`
source with a single Event member is downgraded to `push`. It is a Gate-side
evidence fact carried on the taxonomy record, not a model judgment, and it is
not recomputed inside `decide()`. Since #501 taxonomy is the second of three serial Predictors
(`event_semantics -> taxonomy -> reader_card`); the common successful production
route is exactly three physical model calls, and the taxonomy call reads no
told ledger. #117's "not a third Predictor" decision is withdrawn by #501: the
independent Predictor is what lets GEPA optimize the classification text alone
while EventSemantics and ReaderCard stay byte-identical.

Migration `20260829_0328` trips open canaries and records the identity and prior
evidence disposition. Review v5 and older Program evidence remains append-only
audit history and cannot enter Review v6 denominators. Worker startup opens the
new bundle-owned epoch. Rollback restores the prior exact image/bundle; it never
deletes or rewrites taxonomy judgments, reviews, or receipts.
