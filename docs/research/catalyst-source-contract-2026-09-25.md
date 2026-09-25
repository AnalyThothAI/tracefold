# Catalyst source contract correction

Baseline: `0dad330305546610e612720a0b11e3ec38ceb55c`. Related work: #690 and #691.
This note records a narrow defect correction, not a second implementation plan.

## Provenance and root cause

`news/pipeline/triage.py::_persist_prepared_settlement` maps the current News
editorial `headline_zh` and `why_zh` to public outbox `headline` and `why`.
That mapping is identical at `c69b6bc` and this baseline, before and after #692.
The News-internal names are still live business fields, not obsolete database
columns. No production database inventory was performed for this correction.

#692 introduced `build_event_price_candidates`, whose catalyst qualification
instead looked for `headline_zh/title/why_zh`. Its strategy fixture supplied
`title`, so it did not exercise the producer's actual payload. Evidence rendering
later accepted the public keys alongside those aliases, but qualification was
not updated. One source could therefore be citable yet ineligible for both an
entry and WATCH. This is a consumer-contract mismatch, not evidence of a needed
legacy Trading payload version.

## Correction

The existing pure feature owner provides `catalyst_text_values`. Both initial
candidate qualification and FrameReader source evidence use its nonblank string
values from `headline/why` only. Original strings and raw source facts are not
mutated. Internal aliases cannot supply missing public text; non-text values and
whitespace-only text cannot make a source available. OI numeric zero values keep
their separate existing semantics. The replay summary no longer falls back to
`title`. Tests start at the actual News outbox-producing method and exercise the
FrameReader, archive, evidence menu and final compiler, with external storage and
market ports faked. These are contract tests, not a PostgreSQL or venue receipt.
The compiler and cohort replay fixtures also use public `headline` text; remaining
Trading test uses of the old spellings explicitly verify that aliases are rejected.

## Historical data and rollout

There is no new outbox schema, alias converter, feature flag or database rewrite.
Keep News editorial fields, immutable Triggers, Decisions, attempts and archives.
Read-only historical replay must not recompute an old decision. Already frozen
attempts keep their original inputs; do not rebuild candidates, refresh a root
TTL, replay old sources or issue retrospective Signals to make history look fixed.
Newly prepared cases use the correction after the normal explicit deployment.
Publication settings, execution authority, OI policy and price rules are unchanged.
The separate 16-bar/60-minute feature defect remains tracked in #690.
