# Issue 663: engineering and evidence audit

Audit date: 2026-09-19 UTC. Source baseline: `c8c89065928de2a225cd502cbe40196bffe3609c`.
This report supports [#663](https://github.com/AnalyThothAI/tracefold/issues/663),
continuing [#651](https://github.com/AnalyThothAI/tracefold/issues/651). It is not a model-quality
or production-activation receipt. The deployment was queried in READ ONLY transactions;
no model calls, review acceptance, production writes, migration or deployment occurred.

## Inherited evidence

Read the original `~/.tracefold/cache/issue651/` files and checked accepted ledger records.
The [machine-readable receipt](issue-663-evidence-audit.json) includes source-file hashes,
selected-context/evidence/judgment hashes, per-case lengths and every taxonomy confusion pair.
Receipt SHA: `b5a9ed96c2891441379cb0eab1e1e5c29f427035600dfa1169a533cce1e57c63`.

- 100 batch-1 tasks, 98 successful drafts, 2 draft schema failures.
- 35 accepted judgments and their 35 acceptance records remain intact, all labelled `model_draft`.
- 63 inter-drafter disagreements remain proposals. Axis disagreements overlap: subjects 34,
  event family 17, change state 37, assertion status 17.
- Largest confusion pairs: announced/unknown 12; claimed/confirmed 9; reported/unknown 5;
  announced/effective 5; scheduled/effective 4. These are disagreement counts, not accuracy.
- The inherited 14-case calibration receipt is
  `02f08291c7264163320702b01f38bbf430a9d081e539ef70c49a034da477043e`:
  support accuracy 1.0, key-fact accuracy 0.944444, one faithful-paraphrase coverage disagreement.
  Its old `actual_cost_microusd: 0` does not establish free provider calls. This change adds judge
  diagnostic fields/instructions, so the old receipt does not attest the new judge identity.

## Input coverage inspection

Inspect all 100 batch-1 Event IDs and 20 non-directed controls selected by
`ORDER BY md5(event_id || '663-audit')` among other Events judged since `1789430400000`.
The control selection is reproducible for the audited database snapshot and is not a fresh held-out test.
Two controls use the historical editorial v2 contract: their context hashes were checked without
converting their judgments to the current contract.

| Observation | Batch 1 | Controls |
| --- | ---: | ---: |
| Cases / valid recorded selected contexts | 100 / 100 | 20 / 20 |
| Persisted focus content equals actual model content | 100 | 20 |
| Focus title or content truncated at model-input boundary | 0 | 0 |
| Empty model content | 69 | 10 |
| Empty persisted leader Item description | 59 | 9 |
| Nonempty Item description, empty selected focus context | 19 | 2 |

These last two rows do not partition the preceding row: member/focus selection can draw from another
Item. The 21 nonempty-Item/empty-focus examples are explicitly numbered multi-fact stories. The selected
fact is retained as the model title; unrelated numbered facts are not automatically added as its context.
This is selection, not proof of lost supporting evidence. For example, separate Japanese-bank forecasts
retain their own attribution and conditions; ETF warning stories retain each fund's individual warning.

Manual inspection also found a US–China tariff-talk case (`88436bdc50a8…`) where the relevant source
sentence is already in the actual input, while the model's primary asset is crude oil. The surrounding
multi-news context and provider tags contain other instruments. This is an entity-grounding/selection
risk, not an observed model-input prefix loss. The draft's proposed CNY correction remains unaccepted
unless independently reviewed. In `13805d86660a…`, a corporate source's tokenized-asset claim is present;
claimed/confirmed requires attribution adjudication, not additional invented facts.

The sample supports retaining the existing online evidence contract for this engineering cut. It does
not show that prefix truncation is the dominant defect, and it does not establish full provider-document
coverage. The persisted Item projection is not the raw provider envelope; upstream availability and
pre-persistence loss remain unmeasured. Source conflicts and semantic key-fact visibility need independent
adjudication. No historical input was supplemented from a later webpage.

## Engineering boundary

The change freezes selected execution contexts and original accepted values in content-addressed
per-case artifacts, retains all valid cases while splitting by group, supports partial taxonomy,
and shares one supervision projection across planning, examples and reports. Exact historical and
counterfactual sequence protocols are distinct. A sequence requires all post-Event triage inputs,
including unreviewed Events; missing inputs block a claim of complete sequence evaluation.

Semantic explanation reaches stock DSPy 3.3.1 GEPA through the configured metric judge. Run-level
budgeting counts task/reflection/metric_judge physical calls and unknown versus observed cost.
A swallowed metric failure cannot produce ADVANCE. Native tests use scripted provider responses:
they prove wiring and failure behavior, not remote model capability.

## Quality campaign remains separate

The 35 accepted examples are reused without wholesale relabelling. The 63 disputes should first be
stratified by the confusion pairs in the JSON receipt; representative claimed/confirmed and
announced/effective cases need independent source-based decisions. Neither drafter A nor majority vote
is automatically accepted. This inspected audit population must not be described as unseen test.

Before any paid campaign: establish a separately authorized model/call/cost/wall-clock budget,
recalibrate the changed judge, freeze group-disjoint development/selection and independent test sets,
and preregister the minimum useful improvement, allowed severe-error regression, latency/cost limits
and one-run stopping rule. Run one target first, report missing-input/mask denominators, then validate
with predicted upstream inputs and independent acceptance. No reliable improvement means keep baseline.
Publication and production activation require their own authorization and existing release controls.

Engineering delivery, remote model quality and production effect are separate completion states.
This work does not close #663 or #651 on the strength of local tests, inherited calibration or a PR.

## Local verification

On the implementation worktree, using the locked DSPy 3.3.1 environment:

- Hermetic selection including the installed-distribution seam: 2,720 passed,
  865 deselected, 58 subtests. Command: `python -m pytest tests -m "not integration
  and not deploy and not e2e and not golden and not slow and not scheduled and not
  external_codegen" -q --disable-warnings`.
- PostgreSQL 18 integration: 125 passed, using a dedicated disposable server and isolated migrated databases;
  production is not migrated. Review/partial labels, evaluator/freeze, blind drafts,
  retention, HTTP, migration history and migration preflight are exercised.
- Ruff lint/format, mypy (348 source files), generated CLI/RabbitMQ/router checks,
  required document links and Python compilation pass. Database-schema generation at
  migration head has no drift; OpenAPI regeneration has no changes.
- Native GEPA tests execute the locked framework with scripted provider responses,
  including semantic scoring, cache accounting, unknown costs and terminal judge failures.
  They do not measure remote model quality or production performance.

Full golden/broker, browser, deployed-service and paid-model campaigns were not run.
The current console has no correction submission form; the existing HTTP review and
read-only detail surfaces are covered through contract/integration tests.
