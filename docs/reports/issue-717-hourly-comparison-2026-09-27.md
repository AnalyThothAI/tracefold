# Issue 717: News one-hour baseline and offline replay

Source: [Issue 717](https://github.com/AnalyThothAI/tracefold/issues/717).
All production reads below used a read-only PostgreSQL transaction. No production
row was changed and the candidate code was not deployed for this comparison.

## Fixed observation window

2026-09-27 **14:02:27.742–15:02:27.742 UTC** (`adopted_at_ms >=
1790517747742 AND adopted_at_ms < 1790521347742`). The window was fixed before
the implementation replay. Counts include adopted EventUpdates, not raw Items.

| Measure | Production baseline | Candidate offline replay | Interpretation |
| --- | ---: | ---: | --- |
| Adopted revisions / Events | 52 / 45 | Same 52 inputs | No admission or Event identity change. |
| Claim slots across adopted documents | 104 | 102 for the two affected revisions; other revisions unchanged | Each of the two duplicate revisions loses one redundant ref. |
| Extra Claims with identical structured fields | 2 in 2 revisions (0.038/revision) | 0 in those 2 revisions | This is a duplicate proxy, not a semantic identity oracle. |
| Evidence relations in the two affected revisions | 26 + 14 | 26 + 14 | Source relationships survive ref reuse. |
| Public catalyst rows for the two affected revisions | 2 | 0 | Repeated propositions stop being republished as new catalysts. Source updates remain. |
| Semantic observations | 52 | Not measured live | Candidate replay starts from persisted understanding. |
| Evidence snapshot to semantic completion | P50 14.738 s; P95 66.703 s, n=52 | Not measured live | Latest snapshot creation at or before completion is the start proxy; it includes queue and model time. |
| Adoption to sent provider receipt | P50 31.041 s; P95 87.472 s, n=28 | Not measured live | Joined update revision to actual `news_deliveries` sent receipt. |
| Selected refs in sent cards | 49 across 28 sent intents; one card selected two Claims with identical fields | That affected update replays with one ref | The card itself was not regenerated or resent offline. |
| Judgment cache writes | 1,420 | Not measured live | Cache writes are **not** generation calls. |

The production database and worker logs do not provide a reliable count of
generation calls per adopted revision, so that requested metric is unmeasured.
There were no newly failed terminal semantic rows in the fixed hour. At
15:14:50 UTC, the current ledger contained one exhausted `news_generation_output_contract_invalid`
revision (`attempts=3`, wanted 2, done 1). The old pending query counted it as
pending; the candidate query classifies it as `semantic_failed_exhausted=1`,
`semantic_pending=0`, `semantic_deferred=0`.

## Wider duplicate replay

A read-only scan of the preceding 24 hours found seven adopted revisions whose
Claim arrays gained 14 extra entries with exactly equal structured fields.
The original Issue audit identified one further duplicate revision with
different structured fields. Its two source texts and their complete citation
quotes are identical, while the second reading omitted an earlier quantity
and the relation model returned `unrelated`. The narrow repeated-source rule
also reuses that Claim. For each revision, the replay used its persisted
understanding, preceding EventUpdate head, adopted evidence and relation refs.
Unrelated referenced priors were supplied as placeholders only to preserve
their ref for assembly; the
replay evaluated local `equivalent` relations against the actual prior head.
It did not call a model, rewrite the ledger or resend a card.

| Event prefix / input revision | Prior Claims | Original Claims | Replayed Claims | Evidence links, original → replay | Public kind, original → replay |
| --- | ---: | ---: | ---: | ---: | --- |
| `4c237c35ae73` / 2 | 13 | 14 | 13 | 26 → 26 | catalyst + source → source |
| `5607de8273ee` / 2 | 3 | 5 | 3 | 6 → 6 | catalyst + source → source |
| `5a2f57f7234b` / 2 | 1 | 2 | 1 | 2 → 2 | source → source |
| `7a53780408c0` / 2 | 1 | 2 | 1 | 2 → 2 | catalyst → source |
| `97ee2c2a0daa` / 3 | 6 | 11 | 6 | 18 → 18 | catalyst + source → source |
| `ac2a21e5c1ab` / 2 | 2 | 3 | 2 | 4 → 4 | catalyst + source → source |
| `c4fcaa49882a` / 4 | 3 | 6 | 3 | 12 → 12 | catalyst → source |
| `d676f6debe4d` / 2 | 7 | 8 | 7 | 14 → 14 | catalyst + source → source |

The five BUG-D revisions named in the original Issue audit go from **10 extra
Claim refs → 0**. Across all eight replayed revisions, **15 extra refs → 0**,
with every revision retaining the original number of evidence relations.
Seven false catalyst projections disappear.
The last and first rows above are the two revisions in the fixed hour. Exact
structured-field equality is a screening proxy: a genuine A→B→A occurrence can
share fields, so the result relies on the persisted local equivalent relations
and the separate reversal regression test.

## Reader copy and failure recovery

The 10:37:24 UTC sent BUG-C card (outside the fixed hour) rendered a Persian
Claim about publishing photos of a second captured US drone as a second sunk
US submarine. Its adopted `statement`, structured `object` and citation quote
refer to `زهپاد` (drone), with quantity 2. The candidate composer now receives
only that selected claim's statement, fields, exact quote and minimal source
identity, and its instruction explicitly preserves object, action, quantity,
attribution and phase. The frozen regression checks this input contract and a
faithful scripted rendering. A live model output after deployment remains to be
observed; the scripted test does not prove that every model response is factual.

Truncation is now classified from the provider response's structured
`finish_reason=length`, and no raw response sample is logged. A distinct
configured fallback can answer once; without one, a deterministic output or
reference fault fails the revision on its first attempt. Transient provider
failures still use the existing bounded retry path. Unit tests cover these
branches. Production effects on failure rate, model calls and P50/P95 require
an equal-length post-deployment window with the same definitions.
