# ReaderCard fidelity #629: implementation and evidence

Issue: [#629](https://github.com/AnalyThothAI/tracefold/issues/629).
Base: `77350161c2cb9e0a9fd21d20361be3a4ad2079af`.
This is a development study and implementation receipt. It does not establish
a production quality improvement or authorize candidate promotion.

## Change and identities

The existing EventSemantics → Taxonomy → ReaderCard graph and JSONAdapter are
unchanged. EventSemantics no longer assumes a product launch is bullish.
ReaderCard prioritizes source attribution, conditions, execution status, time
basis and units over a minimum length or an invented price mechanism. Its
instruction shrinks from 4,712 to 4,516 UTF-8 bytes; Taxonomy is byte-identical.
Short substantive copy and supported `或将/有望` are valid. Chinese, upper
length limits and typed format fallback remain.

The offline metric reuses the existing factual-support question for failed
factual fidelity and rewritten why support. A supported repair has a denominator
without reference Chinese wording. Literal accepted text needs no question;
literal failed text earns no repair credit. Unsupported and unavailable answers
both score zero with distinct outcomes and shared call accounting. Why value
remains accepted-pass retention or independent human diagnosis; a failed label
without gold is explicitly unscored. GEPA remains taxonomy-only.

| Contract | Candidate |
| --- | --- |
| Program | `ffbb0a1ff4e7363496250971d8a52f3a2e1e813700d2320d05b02a6b9edcfb49` |
| Instruction root | `9de537e16b4acbe3d643bfff030001ac74c407728b2fb32677b445956a51d1a8` |
| Execution envelope | `8aa82c1af1e3273a0fa584b280b8ab2b4d33d86ae32814b6ced66719d8c81235` |
| Lint | `tracefold.news.reader_card_lint_v2` |
| Metric / receipt | `tracefold.news.production_action_trade_relevance_v9` / `compile_metric_receipt.v6` |
| Judge | `tracefold.news.card_equivalence_judge_v4` / `news_metric_judge_v4` |
| Baseline report | `tracefold.news.program_baseline_report.v4` |
| Evaluator | `news_candidate_evaluator_v8` |

#626 is closed and its heldout is over. #622 remains open: this change retains
its current retry budget and provider-cost guardrail; it does not implement
k=3→1 or remove cost checks. #628 entity identity and #630 novelty remain
separate work. Historical datasets and reports retain their original identities
and cannot be reused as current-contract release evidence.

## Frozen evidence and provenance

The [fixture](../../tests/fixtures/news/reader_card_fidelity_cases.json) contains
six production snapshots and three synthetic positive controls, with case root
`f7099327fcc1fcb2a01d9f66c93dfd5b789131b4c405c668c520a039bd458bc6`.
Every case retains the exact bounded model input, including `raw_first_line`,
and the selected historical Told rows. ReaderCard receives no Told history or
Taxonomy output.

| Case | Source boundary / control |
| --- | --- |
| PONS | Third-party claim; revenue on most days; wallet ready for TWAP, not executed buying |
| IREN | Title and empty body; Batch Zero membership supplies no power guarantee |
| HOOD | Bernstein headline and source-prefix repetition; no accounting recognition or EPS evidence |
| Platinum / Ingram | Indicative share offering prices; share origin and cash recipient unknown |
| Bitget CP/SOPH | Exchange attribution exists in `raw_first_line`, despite its absence from the title |
| Confirmed buyback | Completed $12m this quarter and 3% fewer shares are explicit |
| Realized cost | $40m annual fixed-cost reduction and $12m one-time charge have different bases |
| Conditional APR | Variable, pre-fee 12% annual rate dependent on utilization |

All nine cases are **development**, not independent holdout. Platinum and
Ingram form one fact cluster. No discovered external facts were inserted into
the original evidence. HOOD was recovered through the exact persisted
event/evidence-version/evidence-hash join; it is not currently available in the
ReviewDesk task source.

Two existing accepted Platinum/Ingram reviews are preserved verbatim. They came
from model drafts accepted under reviewer `owner_authorized_claude`, mark
factual/headline fidelity pass, and have no why labels. Those pass judgments
conflict with the issue's source analysis and need adjudication. They are not
silently relabelled, and the other cases acquire no invented reviews.

## Live development replay

The existing `run_baseline(mode="runtime_live")` executed the two instruction
arms on the same frozen inputs using operator-owned routes. The old Program is
`32467582665d454b515137f2325746af55bdb0a9c4c29098afe5bbd5d590db0a`.
Both arms use the current execution envelope: this is a controlled instruction
comparison, not a recreation of the whole historical deployment.

| Receipt, nine requested cases per arm | Baseline | Frozen candidate |
| --- | ---: | ---: |
| Answered / fallback answers | 9 / 9 | 9 / 9 |
| Physical provider calls | 30 | 30 |
| Input tokens | 115,926 | 114,721 |
| Output tokens | 2,656 | 2,482 |
| p50 latency, ms | 4,723 | 5,142 |
| p95 latency, ms | 7,922 | 8,038 |
| Wall time, ms | 46,130 | 48,277 |
| Cases with unknown actual monetary cost | 9 | 9 |

The normal successful graph remains three Predictor calls. Each arm recorded
27 successful fallback calls and three failed primary calls before the primary
circuit opened. Qwen's upstream returned `news_program_lm_server`; every answer
came from DeepSeek Flash fallback. This does not verify Qwen quality or normal
primary-route latency. Provider monetary cost was unavailable; tokens are not
a dollar measurement.

The first captured nine-case baseline report contains two taxonomy-projection
construction errors: the diagnostic harness passed the source-authority field
alongside the four model taxonomy axes. Its raw generated cards and performance
receipts remain valid, but its scalar is not a quality comparator. The projection
was repaired before the final candidate replay. Earlier failed/intermediate
runs remain archived, and no failed attempt is renamed successful.

The existing DeepSeek Pro factual-support judge answered for all nine cards per
arm: baseline 2/9 supported, frozen candidate 9/9 supported. These are
**whole-card model-draft diagnostics**, not independent human labels and not
specifically a why-support pass rate. A rejection can concern the headline or
structured judgment too. No model judgment was accepted into ReviewDesk.

| Quality dimension | Existing accepted labels | Effective current evidence | Interpretation |
| --- | ---: | --- | --- |
| Factual fidelity | 2 | Candidate retention 0/2 without semantic judge | Historical labels require adjudication; no human improvement claim |
| Headline fidelity | 2 | Candidate retention 0/2 without semantic judge | Literal retention is not source correctness |
| Why support | 0 | No accepted denominator; 9 pending | Whole-card draft support cannot fill this denominator |
| Why value | 0 | Unscored; 9 pending | No measured usefulness improvement |

The final outputs preserve PONS attribution and readiness, omit IREN power
guarantees and HOOD accounting/EPS claims, leave offering structure unknown,
and keep Bitget source attribution. The positive controls retain their explicit
completion states and numerical bases. These observations describe the selected
development cases only. Reader usefulness, subtle attribution errors and
generalization remain for independent assessment.

## Verification

The initial why-support reproducer failed on the old metric: two cases reported
effective denominator 0 instead of 1. Focused tests then proved support repair,
synonymous preservation, unsupported rejection and unavailable separation.

The [PostgreSQL integration test](../../tests/integration/test_news_reader_card_fidelity.py)
submits through ReviewDesk, persists the review, freezes a dataset, exports its
original evidence and scores through the shared metric. All four scenarios
pass, including shared physical judge-call accounting and unscored why value.
Its scripted provider verifies the transport/accounting seam, not language
quality; the live receipts above supply separate generation evidence.

[Evidence tests](../../tests/news/test_news_reader_card_evidence.py) prove original
input boundaries, source attribution and qualifier retention without a model.
Existing native Program, artifact, lint, judge and baseline checks cover the
three-Predictor graph and contract pins. Prompt-string assertions do not stand
in for generation evidence.

The first complete local preflight found a stale metric-v8 assertion; the next
focused report check also exposed delivery-owned `timeliness` leaking into
prediction diagnostics. Both were repaired. The final complete local preflight
is recorded on the PR with its exact tested tree and native reports; no skip,
xfail, rerun plugin or missing resource is used to claim success.

## Remaining acceptance and fixed windows

The randomized S01–S09 A/B review packet and its separate sealed mapping are
prepared. Independent human judgments are pending; they must retain their
actual reviewer and evidence references through the existing ReviewDesk.
The Markdown packet is preparation, not a substitute accepted pairwise receipt.

The Program was frozen at **2026-09-12 06:01:42.306 UTC**. The predeclared future
sample window is **2026-09-12 06:01:42.306 UTC through 2026-09-13 06:01:42.306 UTC**.
Choose reviewed events strictly after candidate freeze, exclude connected
development clusters, and preserve the existing release profile and thresholds.
An empty or underpowered sample is unknown, not success.
This is an independent diagnostic sampling window. A formal release holdout
must also begin after actual candidate registration; this window cannot be
relabeled as that holdout if registration happens after it began.

A formal CandidateEvaluator paired release run has **not run**: new adjudicated
reviews and a current-contract frozen development dataset are still missing.
Use the existing `news release register` / `news release evaluate` lifecycle,
including its blind pairwise tasks and future validation. Do not convert this
baseline diagnostic into a release PASS or skip prior-stage guards.

Production identity switch has **not happened**. Exact PR HEAD and final main
SHA each need the fixed complete CI. Production observation is a **separate**
fixed 24-hour interval beginning at a verified deployment, with Program/envelope,
runtime/image, route failures, physical calls, latency and reviewed-dimension
denominators captured. The future sampling window above is not that receipt.
A non-significant result must remain unproven; do not tune thresholds or choose
another window until it looks green. #629 remains open until its actual
acceptance evidence is recorded.

## Artifacts and reproduction

The companion
[development receipt](news-reader-card-fidelity-629-2026-09-12.json) preserves
sanitized outputs, source hashes, per-arm usage, judge identities and the study
protocol. The baseline Program remains available at its original commit and
in the archived `baseline-program.json`; only the current stable artifact is
packaged at runtime.

Raw reports, traces, unsuccessful attempts, the review packet and its fixed
mapping are retained under the operator-local research archive
`/home/qinghuan/.tracefold/research/issue-629`. Task-local working copies live
under `artifacts/issue-629`. The archive contains no copied credentials.
The baseline replay harness delegates to existing runtime and baseline owners;
the judge harness delegates to existing `facts_supported`. Neither is an
alternative optimizer, judge service, review store or release authority.
