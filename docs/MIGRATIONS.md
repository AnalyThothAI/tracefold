# PostgreSQL migrations

This is the migration authoring and hard-cut audit guide. PostgreSQL remains
the only schema authority; this document is review evidence, not a runtime
registry.

## Authoring contract

Create a revision with Alembic and replace every `TODO` emitted by
`tracefold/platform/postgres/alembic/script.py.mako`. A revision is warranted
only for a table, column, view, index, function, trigger, constraint, role,
grant, or durable-data change. Python refactors, policy/model/release identity,
timeouts, batch sizes, and presentation changes do not create revisions.

Every revision records:

- category and why PostgreSQL must change;
- current and minimum supported source revision;
- lock level/order plus statement and lock timeouts;
- estimated rows/bytes and rewrite or index-build behavior;
- preflight and maintenance boundary;
- archive/current compatibility and role/grant impact;
- explicit failure state and roll-forward or verified backup-restore path;
- the exact production PostgreSQL major, image family, and digest used by its
  evidence.

Required evidence is empty database through every revision to the single head,
head-to-head no-op, affected historical fixtures, exact schema/role/grant/
index/function contracts, and a bounded lock/rewrite/row-count test for a
destructive or backfill revision. Required PostgreSQL lanes use the same exact
PostgreSQL 18 Bookworm image digest as production. Business processes remain
stopped behind the maintenance gate until migration succeeds. Published
revision files are immutable; corrections are forward revisions. Downgrade of
an irreversible hard cut is a verified backup restore, not invented reverse
DDL.

## 0330–0332 authority audit

Audit-start baseline: exact `main` `17338e78dbf0c99dc98e876433790adf02e1bd70`,
head `20260830_0333`. The implementation was reconciled with
`main@651ba997d43d0046660ac5dd84f221782b6e1a63` / `20260830_0334`, then added
forward revision `20260830_0336` to freeze explicit current-view columns. The
audit found no object that can be removed
without changing the News current contract or Trading capital/execution truth,
so no published revision was edited and no forward-removal revision is
warranted. Every object below is retained. Objects grouped in one row inherit
every field in that row; each exact object name is listed so the contract test
can fail closed on omissions.

Cost evidence uses PostgreSQL-native statement/transaction timeouts and plan
rows/blocks rather than shared-runner wall-clock thresholds. The relevant
real-PostgreSQL seams are
`tests/integration/test_news_learning_migration.py`,
`tests/integration/test_trading_migration.py`,
`tests/integration/test_postgres_contract_authority.py`, and
`tests/integration/test_trading_transaction_cpu_boundaries.py`.

### 0330 — News current contract

| Database objects | Table/write path and protected invariant | Independent writers and matching Python owner | History, frequency, payload, and cost | Decision |
|---|---|---|---|---|
| `news_canonical_jsonb` | Current verdict/editorial/evidence/review content hashes must address the exact persisted JSON bytes. | Triage, deterministic telemetry judgments, evidence snapshot writers, and ReviewDesk; `tracefold.news.artifact_identity.canonical_sha`. | Archive rows are not revalidated. Current writes are per admitted Event/review; bounded JSON. Canonical hash equivalence and native timeout insert/update budget are tested. | Retain: cross-language identity belongs beside persisted bytes. |
| `news_jsonb_exact_keys`<br>`news_jsonb_ordered_string_set_valid`<br>`news_jsonb_required_optional_keys`<br>`news_jsonb_int64_valid`<br>`news_jsonb_forbidden_keys_absent` | Closed JSON wire shape, canonical code ordering, integer range, and recursive removal of retired keys for current facts. | Model Triage plus deterministic OI/liquidation writers and ReviewDesk; exact Pydantic models under `tracefold.news.models`, `program.contracts`, and `review.desk`. | Helpers run only through current-row constraints; archive rows remain audit-only. Bounded arrays/objects and equivalence mutation corpus are tested under native timeout. | Retain: multiple writers must share one fail-closed persistence fence. |
| `news_current_told_trace_valid`<br>`news_current_triage_verdict_valid`<br>`news_current_model_editorial_valid`<br>`news_current_decision_valid` | Verdict, editorial, told ledger, decision, and their hashes form one current `news_judgment_v2` atom. | Model Triage, degraded/direct-rule Triage, OI, and liquidation paths; `TriageVerdict`, `EditorialEnvelope`, `ScoredJudgment`, and decision models. | One verdict per stage/policy identity; at most 16 told entries, eight assets, and contract-bounded copy. Valid/invalid cross-language corpus and production-size native timeout budget cover drift/cost. | Retain: a malformed direct SQL writer would otherwise become durable judgment truth. |
| `news_current_oi_signal_valid`<br>`news_current_oi_metadata_valid`<br>`news_current_liquidation_fact_valid`<br>`news_current_liquidation_metadata_valid` | Deterministic market judgments must carry typed parsed facts, source identity, and decision consistency. | OI and liquidation deterministic writers are distinct from model Triage; Python owners are `tracefold.news.oi_signals` and `tracefold.news.liquidations`. | Per admitted telemetry frame; small bounded objects. Historical pre-cut rows are archive-only. Migration fixtures cover accepted/rejected shapes under revision timeout. | Retain: independent deterministic writers share the verdict table. |
| `news_current_evidence_snapshot_valid` | Evidence version, Event/focus-fact identity, provenance, and content hash must agree with the immutable snapshot. | Admission/Triage evidence writers and direct fixture/import paths; Python owner is News evidence storage. | One small-to-medium snapshot version per Event; archive Events reject current evidence. Hash equivalence and real inserts are tested. | Retain: evidence identity is release authority, not presentation validation. |
| `news_current_review_dimensions_valid`<br>`news_current_review_novelty_valid`<br>`news_current_review_evidence_refs_valid`<br>`news_current_review_taxonomy_valid`<br>`news_current_review_expected_valid`<br>`news_current_review_taxonomy_provenance_valid`<br>`news_current_event_review_payload_valid`<br>`news_current_pairwise_review_payload_valid`<br>`news_current_review_selection_valid`<br>`news_current_review_valid` | Accepted event/external-miss/pairwise review rows must keep rubric, expected answer, evidence references, selection probability, taxonomy, and provenance mutually consistent. | Serve-role ReviewDesk append, CLI review/import, and learning readers; matching exact models live in `tracefold.news.review.desk`. | Append-only human judgments, much less frequent than Event ingestion, contract-bounded payloads; old review versions remain archive-only. Historical fixtures and valid/invalid payload tests run on real PostgreSQL. | Retain: Serve can append directly with narrow grants and must not bypass review integrity. |
| `news_current_verdict_evidence_guard`<br>`news_verdicts_current_evidence_check` | A current verdict must reference the exact current evidence version/SHA/focus fact for its Event. | Every verdict producer; Python storage prepares the identity but PostgreSQL owns the cross-table fact. | One indexed lookup per current verdict; archive verdicts are excluded. Real migration fixtures prove mismatch rejection. | Retain: cross-table identity cannot be enforced by Pydantic. |
| `news_current_event_archive_guard`<br>`news_events_current_archive_only_check`<br>`news_reviews_current_archive_only_check`<br>`news_event_evidence_current_archive_only_check` | A pre-cut archive Event, review, or evidence row can never be resurrected into the current contract. | Workers, ReviewDesk/Serve, maintenance, and migration/import paths. | Trigger is O(1) by Event key. Old rows remain readable only as audit evidence. | Retain: current/archive is a database authority fence. |
| `news_current_review_acceptance_target_guard`<br>`news_reviews_current_acceptance_target_check` | An accepted review targets a current eligible Event/evidence identity. | Serve/CLI review writers and News learning readers. | Append-time indexed lookup; reviews are low-frequency and permanent/retained evidence. | Retain: cross-table acceptance authority. |
| `news_current_review_source_exists`<br>`news_current_review_source_guard`<br>`news_reviews_current_task_source_check` | Review task source, subject kind, and selected Event/external miss/pair are real and current. | Serve/CLI review appenders. | Low-frequency indexed existence checks; old tasks stay archive-only. | Retain: protects foreign identities not expressible as one ordinary FK. |
| `news_events_source_contract_reason_check`<br>`news_events_source_contract_consistency_check`<br>`news_verdicts_final_decision_check`<br>`news_verdicts_current_judgment_check`<br>`news_event_evidence_current_contract_check`<br>`news_verdicts_current_evidence_fk`<br>`news_events_current_focus_fact_check`<br>`news_reviews_current_contract_check` | Current Event, verdict, evidence, and review scalar/JSON/cross-row invariants; `NOT VALID` preserves historical rows without admitting new invalid facts. | News admission/Triage/ReviewDesk plus deterministic telemetry writers; matching Python owners are the current News models and storage methods. | Checks run per current write; bounded payloads. The 0329→0330 fixture proves history preservation and invalid-write rejection. | Retain: these are the composed current-contract authority. |
| `news_current_events_v1`<br>`news_review_task_source_v1`<br>`news_review_records_v1` | Public/current projections exclude archive-only Events and non-current judgments/reviews. | Serve, ReviewDesk, learning, and Trading handoff readers. | Hot reads use explicit columns and production predicates; plan audit covers current filters. | Retain: one current projection prevents reader-specific archive leakage. |
| `ux_news_event_evidence_current_identity`<br>`ix_news_events_current_opened` | Unique current evidence identity and ordered current-feed access. | All current evidence writers/readers and public feed. | Partial indexes exclude archive rows; real plan evidence binds them to current query builders. | Retain: read/write evidence supports both indexes. |

### 0331 — Trading Production V3

| Database objects | Table/write path and protected invariant | Independent writers and matching Python owner | History, frequency, payload, and cost | Decision |
|---|---|---|---|---|
| `trading_jsonb_object_size`<br>`trading_capability_snapshot_shape_check` | Capability payload partition counts, venue/binding/catalog identity, and included/excluded objects agree. | Workers capability compiler and operator/migration restore paths; `tracefold.trading.capabilities` and storage capability owner. | Catalog compilation is infrequent; payload can contain production-size instrument sets. It is prepared/hashed outside the transaction, persisted in one statement, and covered by the 20k-instrument native idle-timeout regression. | Retain: catalog bytes and scalar indexes must agree at the persistence authority. |
| `reject_new_execution_capability_v1`<br>`trg_trading_capability_v2_only` | No post-cut V1 capability snapshot can be inserted. | Workers compiler and any operator/import path. | O(1) per infrequent snapshot; V1 rows remain immutable archive facts. | Retain: hard-cut generation fence. |
| `reject_new_legacy_trade_intent`<br>`trg_trading_intents_v3_only` | New V1/V2 intents are forbidden after V3 cutover. | Workers owns intent creation; Nautilus owns execution lifecycle updates. | O(1) on new intents; legacy rows remain archive facts. | Retain: two processes share the same durable Intent table. |
| `trg_trading_execution_bindings_append_only` | Execution binding evidence is immutable. | Workers/compiler and restore/import paths; `tracefold.trading.storage.capabilities`. | One row per account generation/binding, small payload, infrequent. | Retain: append-only execution authority. |
| `trading_capability_catalog_fk`<br>`trading_binding_capability_fk`<br>`trading_binding_execution_binding_fk`<br>`trading_intents_execution_binding_fk`<br>`trading_intents_venue_catalog_fk` | Capability, active binding, Intent, and venue catalog identities form one referential chain. | Workers catalog/capability/Intent writers and Nautilus Intent updater. | Indexed identity lookups; immutable referenced facts. | Retain: cross-table execution authority. |
| `trading_binding_account_generation_check`<br>`trading_binding_capability_state_check`<br>`trading_binding_capability_pair_check`<br>`trading_binding_capability_error_check` | Runtime binding generation, readiness, snapshot timestamps, and bounded error text are consistent. | Runtime discovery/reconciliation and operator control; Python runtime binding model. | One small current row per binding, updated on discovery/reconnect. | Retain: independently updated runtime state must fail closed. |
| `trading_execution_binding_sha_check`<br>`trading_execution_binding_binding_check`<br>`trading_execution_binding_generation_check`<br>`trading_execution_binding_payload_check` | Immutable execution-binding scalar columns match its versioned payload and content identity shape. | Workers capability compiler and restore/import. | Infrequent, sub-KiB payload, append-only. | Retain: durable binding evidence. |
| `trading_intents_version_check`<br>`trading_intents_current_shape_check`<br>`trading_intents_submission_fence_v1_check` | Current Intent version, required V3 identities, and entry submission fence fields are coherent. | Workers inserts; Nautilus progresses lifecycle. Python owner is `tracefold.trading.intent.TradeIntent`. | Per admitted trade, small bounded payload/columns. Real migration and execution tests cover each state. | Retain: safety invariant shared across two processes. |

### 0332 — Trading capital authority

| Database objects | Table/write path and protected invariant | Independent writers and matching Python owner | History, frequency, payload, and cost | Decision |
|---|---|---|---|---|
| `trading_runtime_arm_epoch_check`<br>`trading_binding_active_arm_fk`<br>`trading_intents_capital_authorization_fk` | Runtime arm generation and every Intent's capital authorization point to immutable current authority evidence. | Operator arm action, Workers capital reservation/authorization, and Nautilus Intent lifecycle; `tracefold.trading.capital_lane` and storage authority owner. | Indexed scalar lookups per arm/Intent; history is immutable. | Retain: cross-process capital fence. |
| `trading_intents_funding_check`<br>`trading_intents_reason_check`<br>`trading_intents_state_shape_check` | Settlement cash-flow JSON is bounded/typed and reason/state fields match execution lifecycle. | Nautilus updates execution/settlement while Workers creates Intent; `TradeIntent` is the matching Python model. | Per lifecycle transition, funding JSON capped at 2 KiB. Shared valid/invalid corpus and native timeout write budget cover drift/cost. | Retain: the execution adapter is an independent writer. |
| `trading_risk_policy_sha_check`<br>`trading_risk_policy_clock_check`<br>`trading_risk_policy_payload_check` | Daily risk policy identity, validity window, and payload/scalars agree. | Operator promotion/configuration and Workers authorization reader; capital-lane policy models. | Infrequent append, small payload, permanent evidence. | Retain: policy is capital authority. |
| `trading_promotion_grant_sha_check`<br>`trading_promotion_grant_binding_check`<br>`trading_promotion_grant_clock_check`<br>`trading_promotion_grant_payload_check` | Promotion grant identity, binding, clock, scope, result, and risk policy agree. | Operator release action and Workers capital reader. | Infrequent append, small payload, permanent evidence. | Retain: promotion authority must survive application defects. |
| `trading_grant_revocation_sha_check`<br>`trading_grant_revocation_clock_check`<br>`trading_grant_revocation_payload_check` | A unique revocation has valid identity/time and names its grant. | Operator revocation and Workers reader. | Infrequent append, sub-KiB payload. | Retain: fail-closed revocation evidence. |
| `trading_arm_receipt_sha_check`<br>`trading_arm_binding_check`<br>`trading_arm_epoch_check`<br>`trading_arm_clock_check`<br>`trading_arm_payload_check` | Operator arm receipt binds exact account generation, grant, capability, binding, epoch, and time. | Operator arm command and Workers reader. | Infrequent append, small payload. | Retain: arming is explicit capital authority, not UI state. |
| `trading_risk_reservation_sha_check`<br>`trading_risk_reservation_binding_check`<br>`trading_risk_reservation_day_check`<br>`trading_risk_reservation_asset_check`<br>`trading_risk_reservation_amount_check`<br>`trading_risk_reservation_payload_check` | A reservation has exact binding/day/asset/positive amount and matching payload. | Workers capital lane and recovery/import. | At most one reservation per admitted capital attempt; small payload. | Retain: prevents oversubscription and split identity. |
| `trading_authorization_receipt_sha_check`<br>`trading_authorization_binding_check`<br>`trading_authorization_payload_check` | Authorization receipt matches reservation, policy, grant, arm, capability, and binding. | Workers authority transaction and Nautilus Intent consumer. | One append per authorized Intent, small payload. | Retain: immutable proof of permission to risk capital. |
| `trading_risk_state_attempt_day_check`<br>`trading_risk_state_amount_check`<br>`trading_risk_state_status_check`<br>`trading_risk_state_terminal_amount_check`<br>`trading_risk_state_settlement_check` | Mutable reservation state cannot cross UTC attempt day, go negative, or claim terminal settlement with incomplete amounts. | Workers reservation and Nautilus settlement/recovery. | Small row updated at bounded lifecycle transitions. | Retain: shared state machine and capital conservation. |
| `trading_risk_event_sha_check`<br>`trading_risk_event_kind_check`<br>`trading_risk_event_amount_check`<br>`trading_risk_event_settlement_check`<br>`trading_risk_event_payload_check` | Append-only capital events have typed kind, signed/positive amount semantics, settlement identity, and matching payload. | Workers authorization plus Nautilus execution/settlement. | A bounded handful per Intent; small payload, permanent ledger. | Retain: capital ledger integrity. |
| `trg_trading_daily_risk_policies_append_only`<br>`trg_trading_promotion_grants_append_only`<br>`trg_trading_grant_revocations_append_only`<br>`trg_trading_operator_arm_receipts_append_only`<br>`trg_trading_risk_reservations_append_only`<br>`trg_trading_authorization_receipts_append_only`<br>`trg_trading_capital_risk_events_append_only` | Policy, promotion, revocation, arm, reservation, authorization, and capital-event evidence cannot be rewritten or deleted. | Operator, Workers, Nautilus, and restore/import paths. | O(1) trigger per append-only mutation attempt; normal path only inserts. | Retain: immutable capital truth is a database invariant. |

### Audit disposition

- Retained: every 0330–0332 object above, because it protects identity,
  current/archive separation, an append-only ledger, cross-table truth, or a
  contract shared by independent writers.
- Governed: SQL placement/dynamic composition, explicit projection columns,
  production/audit statement sharing, and transaction CPU boundaries.
- Optimized: bounded raw retention and band expiry, risk-based query plans,
  fast operational audit, and production-image restore evidence.
- Deleted: no database object and no published revision; evidence did not
  justify a contract-changing forward removal.
- Product-owner follow-up: none created by this audit. A future proposal to
  weaken News current validation or Trading capital/execution authority must
  be a separate product Issue with its own migration evidence.
