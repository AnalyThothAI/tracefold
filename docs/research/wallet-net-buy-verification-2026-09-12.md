# Wallet net-buy implementation evidence (#641)

Scope: replace the wallet product with token-level concentrated net-buy episodes; deployment and notification restoration are separate operations.

## Observed F2P and affected seams

- On base a37cb7abf4b23fcafee4141ecdf20e9d33b47df2, the predecessor real PostgreSQL crowding query returned 1200 for buys 600 + 600 minus sell 300; required net is 900. The replacement is covered by tests/integration/test_wallet_net_buy.py::test_buys_600_600_sell_300_are_net_900.
- Additional observed failing-to-passing races: test_gap_discovered_after_intent_before_detector_cannot_send_old_affirmative_snapshot and test_missing_previously_committed_overlap_log_records_gap_without_erasing_facts. The first send reads locked coverage as well as the persisted event; overlap checks include committed but not yet confirmed tail facts.
- Real PostgreSQL receipt/episode/notification tests cover complete receipt atomicity, unknown sell/transfer exclusions, rollback/restart, chain-time expiry, zero unchanged writes, effective-buy renewal, 3→2→3, both windows, muted restoration, stale/future/cutover, known-at roster support, price failure/late/unknown, retry snapshot equality, and ambiguous send recovery.
- The predecessor migration test preserves raw event/check/outcome/state JSON and frozen attempted/sent/unknown delivery evidence through migration and subsequent sender lifecycle actions.
- Actual PostgreSQL + RabbitMQ + Workers + FastAPI + Chromium smoke: 3 passing tests, including a persisted net-buy episode and adjacent News/liquidation/OI/Trading evidence. Four viewport wallet interaction checks: 12 passed.
- Same ReaderCard through real Feishu/Telegram serialization, with frozen retry equality, unsafe text, tiny prices and adjacent market families.

- Additional observed F2P: unproven pre-cut rosters expanding the collection pool; negative/zero/tiny amounts being formatted as missing/zero; a 390px mobile snapshot extending to x=632; and an already-sent detail appending a contradictory “not sent” for a null reason. The respective real PostgreSQL, component and browser regressions now pass. Mobile paragraphs fit the viewport; wide transaction/member tables retain their own horizontal scrolling.

## Measured bounded replay

The executable test is tests/integration/test_wallet_net_buy_replay.py. The saved JSON contains complete EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output for the 30m hot-token read and pending receipt read.

Synthetic input: 10,000 old context fills and 217 evaluated fills in 212 complete receipts; 10 quality roster members known one hour before the trigger, six hot-token participants. Result: 1 episode, 1 logical first intent, 0 duplicate new fills, 0 unchanged snapshot writes. One unpriced sell and one transfer exclude their affected wallets; three round trips end with zero net quantity. Three stale receipts produce one stale threshold-crossing reason. One missing trigger baseline, zero comparable outcomes.

The hot-window plan returns 200 rows and filters 17 others in 0.163ms. The pending-receipt plan returns 20 complete receipts in 1.917ms, but reports 10,020 heap fetches after the synthetic history was marked derived. This is measured transient index/heap work, not proof of constant-cost backlog reads or a substitute for checking live plans after cutover.

Twelve detector turns: total 1794.692ms, maximum 193.135ms. Offline replay start to the test sender: 2000.954ms. Event, receive, detection, intent and attempt clocks are controlled; these timings do not measure live network latency, daily signal frequency, profitability or an end-to-end SLO.

## Hard cut and deployment

Current active paths have no retired wallet research/digest/exit/crowding implementation, DTO union, configuration alias or /cards forwarding route. Raw buy/sell/transfer_out facts, shared formatting, generic News Program resources and immutable historical migrations remain.

The 0376 migration creates one lossless historical archive and replaces episode/outcome structures. The offline configuration tool preserves unrelated settings and switches and never runs from the Settings loader. The 60-second price-sampling maximum delay is an engineering bound; unavailable trigger baselines and late target prices stay unknown.

Read-only host inventory on 2026-09-12 found Serve/Workers and the order Runtime sharing one operator config. Their existing images were retained. No production migration, restart, trading action, or wallet notification restoration occurred. Follow docs/wallet-net-buy-cutover.md with the exact final main CI receipt before a separately coordinated deployment.

## Verification recovery

The one `make test-ci` attempt is **FAIL/PARTIAL**, not PASS. It stopped in quality-static. The remaining fixed owners were executed separately; failures were diagnosed and the affected nodes were revalidated. One initial runtime-owner execution was interrupted to repair a known process-fixture argument; its partial output was retained.

| Owner / check | Initial result | Repair evidence |
| --- | --- | --- |
| Static Python/generated/docs | Passed; mypy 331 source files | Later touched files checked again |
| Quality | 374 passed, 1 failed | Add the new public snapshot contract to the explicit allowlist; affected module 9 passed |
| Python hermetic | 2175 passed, 2 failed | Register the new migration in the Git index used by package manifests; both package nodes passed |
| PostgreSQL behavior | 513 passed, 5 failed | Update retired table expectation and repair the Workers image-digest fixture argument; 33 affected/adjacent nodes passed. Two broker nodes passed after process-local proxy bypass was corrected |
| Migration | 43 passed, 5 failed | Advance current-head/irreversible-cut test pins to 0376, preserving all predecessor pins; five failed nodes passed |
| Runtime / broker | 30 passed, 4 failed | Container restart changed its private IP. Revalidate using fixed task-owned published ports: all four failed real-broker nodes passed |
| Deploy / scale / Serve process | 114 passed | No repair required |
| Frontend Python / harness | 1 + 64 passed | No repair required |
| Frontend architecture | 22 passed, 1 failed | Replace four literal type sizes with existing global tokens; CSS owner 16 passed |
| Frontend unit/components/routes | 232 passed, 1 failed | Update the route's expected heading and events endpoint; route owner 13 passed |
| Browser golden paths | 68 passed, 4 failed | Exact text locator included the raw-quantity child; corrected to the complete accessible cell name. Subsequent visual F2P repairs also passed all 12 wallet cases on four viewports |
| Real full stack | 3 passed | PostgreSQL, RabbitMQ, actual Workers, FastAPI and Chromium; no mock database or broker |
| Final wallet rendering/CSS | 22 passed | Six wallet component cases plus 16 CSS owner cases; typecheck, affected ESLint, format and build passed |

No skip, xfail, automatic retry, report rewriting or removed required owner was used to manufacture green. The first failed native reports remain under `artifacts/test-results/`; focused repair reports are separate under `artifacts/wallet-net-buy/`, including `migration-broker-repairs.xml`, `final-wallet-component-css.json`, `final-wallet-browser.json` and screenshots. Selection exclusions from the repository's fixed owner partition are not skipped test outcomes.

The actual F2P/P2P nodeids are in `tests/integration/test_wallet_net_buy.py`, `tests/integration/test_news_chain_tape.py`, `tests/integration/test_wallet_buy_migration.py`, `tests/integration/test_wallet_net_buy_replay.py`, `web/tests/component/features/news/NewsWalletsPage.test.tsx` and `web/tests/e2e/golden-paths/wallet-net-buy.spec.ts`. Exact commit, PR, final-main CI and deployment disposition belong to the PR and Issue #641; local results do not replace that merge evidence.
