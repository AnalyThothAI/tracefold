# #791 PR-B: English speech readings and reader v3

This is a draft result. The current code implements B1/B2 and deletes live retired-mode and archived-reader parsing branches. The initial real E1 run has failed gates; E2 is still being completed. These results do not authorize merge or deployment.

[Machine-readable evidence](news-791-b.json) records model names, input hashes, counts and limitations. Extraction and generated judgment used the operator-selected qwen3.8-27b and qwen3.8-27b:judge routes; reader reasks use the configured JEV route and separately the generated route. Independent Claude annotations see public claims and source quotes, without scores, speech readings or production outcomes. No store, judgment cache or sender is constructed by the reask tool. Sources and actual sent bodies were exported with bounded read-only queries.

## Initial E1 result

The first speech run asked 853 claims: the 129 manually categorized promotion rows, the archived day speech sample, and missing cluster examples. An additional reask covers the entire 1,100 day sample and all 397 regression claims. Batch contract failures are recorded, then explicitly retried in smaller batches; they are never converted into positive proof.

The initial exact Issue rubric classifies only 48/77 pure promotion rows as promotion, below the required 73. The 60 complete Event extraction calls all returned usable output, including 30 Chinese/Russian source Events. Only 87/89 claims have English nonquote fields (97.75%, required 98%). Only 42/60 Event counts stay within 10% of the production final understanding count. All source spans were grounded with the real extraction validation. Original frozen source asset candidates were unavailable, so the count comparison has that reconstruction limitation.

The JSON evidence gives true-launch, official-role, opinion, fused/reask, independent numeric and threat-condition measurements. The archive's four broad cluster lists contain distinct claims (including calendar rows and attack attribution), so they cannot establish the required five targeted story gates. Targeted sequential replays must use PR-A's final shared rank and its accepted embedding calibration.

## E2 and release gates

The native v3 reask has produced more than 1,200 available answers on the exact configured `typesafe/jev-1.13-20260917` route. The separate generated run and 1,497 independent blind labels are in progress. On an explicitly partial 610-label diagnostic, official expected-value AUC is .849; at the initial native cut, official keep recall is .726, official demote push rate is .20 and key precision is .384. The latter two fail. Partial weighted volume is not an all-day result.

`ReaderCuts` currently holds the Issue's initial grid values. No calibrated triple has been accepted. Completion requires both backend fits, the selectively relabelled 397 regression set, full day measurements, five sequential story clusters, independent repeat noise and the owner label audit. A good AUC or arithmetic unit test does not replace these gates.

## Engineering validation and migration

The current B implementation passes 1,399 News unit tests, 69 contract checks, mypy over 152 affected files, and the frontend typecheck. The new 0427 migration passed on an isolated PostgreSQL 18.6 fixture clone. Broader integration produced 90 passes and four failures: three old PR-A recall/generation assumptions need the next A-base commit; the retired-mode fixture was converted to opinion and passed its rerun. CI is not reported as passed.

The first B0 commit expanded readers before changing prompts. The final branch retains only current v5 speech values and v3 reader input. Forward migration 0427 explicitly converts stored commentary to unknown and conditional_threat to threat across analyses, frozen receipts/checkpoints and pending public outbox payloads. It preserves source quotes, claim/update identities, input provenance, sent body and outcome; the public payload digest follows the converted structured reading. The migration drains deferred constraints and restores both immutable guards before transaction commit. Before deployment, stop writers and verify a backup. Downgrade requires restoring that backup and its matching image.
