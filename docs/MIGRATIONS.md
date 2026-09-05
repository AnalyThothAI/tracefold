# PostgreSQL migrations

PostgreSQL plus Alembic's single head is the only schema authority. Runtime
processes never execute DDL.

## Current baseline

`20260831_0340` is both the single Alembic root and head. It is a reviewed
current-schema baseline with `down_revision = None`; a fresh PostgreSQL 18
database reaches the complete current schema in one step. The baseline creates
application tables, sequences, views, indexes, functions, triggers,
constraints, and only the structural singleton rows required on an empty
cluster. Extensions remain the empty-PGDATA bootstrap's responsibility.

This squash is the operator-authorized exception recorded by issue #449. This
source may merge or deploy only after every supported pre-cut database is
advanced with the recorded old image to exact terminal head `20260831_0340`, a
verified backup receipt is recorded, and the stopped-writer catalog cut passes.
The terminal identity is then reused while revisions
`20260818_0275` through `20260831_0339`, the old contents of `0340`, and the
role/ACL bootstrap SQL were removed. An already-stamped database therefore
does not replay baseline DDL or rewrite business rows. Git history and the
recorded pre-cut image remain the recovery authority for a pre-baseline backup.

The stopped-writer one-time catalog cut must rename `tracefold_owner` to
`tracefold`, remove the Serve/Workers/Nautilus roles and their ACL/default-ACL
entries, and preserve the terminal revision, normalized schema fingerprint,
row counts, and business identity aggregates. Its mismatch preflight must run
before catalog writes. The operator records the exact old SHA/image, backup,
before/after identities, and startup smoke receipt; current source contains no
cutover helper, old-role repair, fallback, dual head, or compatibility path.

## Authoring contract

After the baseline, every real schema or durable-data change is a new linear,
forward-only revision. Published revisions are immutable; a correction is a
new revision. A revision is warranted only for a table, column, view, index,
function, trigger, constraint, or durable-data change—not for Python
refactors, timeout values, presentation, or application identities.

Every revision records:

- why PostgreSQL must change and the current source revision;
- lock level/order, statement and lock timeouts, estimated rows/bytes, and
  rewrite or index-build behavior;
- preflight, maintenance boundary, failure state, and roll-forward or verified
  backup-restore path;
- archive/current compatibility and the exact PostgreSQL 18 image used by its
  evidence.

Required evidence is a fresh database to head, head-to-head no-op, the smallest
historical fixture affected, and bounded real-PostgreSQL checks for destructive,
locking, or backfill behavior. Business processes remain behind the maintenance
gate until migration succeeds. An irreversible downgrade is a verified backup
restore, never invented reverse DDL.

## Operator archive before a destructive revision

A revision that deletes durable data refuses to run rather than deleting it for
the operator. Three revisions do this today, and all expect the same archive
step first: a `pg_dump` of the affected rows into `~/.tracefold/backups/`, taken
after the writers are stopped by the canonical migration gate, so the dump and
the database cannot diverge between the two.

`20260903_0355` drops the six dead `trading_cases` columns and narrows three
closed vocabularies. It counts the rows that still use a retired value and
raises `trading_retired_values_present` with both totals if any exist. To
upgrade such a database:

```bash
pg_dump --data-only --table=trading_cases --table=trading_candidate_gate_decisions \
  > ~/.tracefold/backups/pre-0355-trading-retired-values-$(date +%Y%m%d).sql

# The admission ledger first: `trading_candidate_gate_decisions.case_id` references
# `trading_cases`, so a `CASE_CREATED` row pointing at a retired-state Case would
# otherwise refuse that Case's delete. Deleting only its retired *values* is not
# enough, because the row that links it is `CASE_CREATED` and not itself retired.
psql -c "DELETE FROM trading_candidate_gate_decisions
          WHERE status = 'RESEARCH_ONLY'
             OR stage IN ('capability', 'catalog', 'routing')
             OR case_id IN (SELECT case_id FROM trading_cases
                             WHERE state IN ('POLICY_REJECTED', 'INTENT_EMITTED', 'ORDER_PREPARED'))"
psql -c "DELETE FROM trading_cases
          WHERE state IN ('POLICY_REJECTED', 'INTENT_EMITTED', 'ORDER_PREPARED')"
```

A retired-state Case can carry no Signal — `enforce_trading_case_signal_link`
allows one only on `SIGNAL_EMITTED` — so the admission ledger is the only
foreign key in the way.

The dump is the only copy afterwards: the six columns' contents go with the
columns, and `downgrade` refuses rather than inventing reverse DDL. Restore it
into a scratch database to read an archived row. `20260901_0347` recorded the
same step for the 22 execution tables it dropped, in
`~/.tracefold/backups/pre-0347-retired-trading-tables-20260901.sql`.

A Case that is still `PENDING` or `RUNNING` is not affected: those states
survive, and the migration gate has already stopped the lane that would settle
them.

`20260903_0356` makes `account_slot` the execution identity. It renames
`trading_execution_observations.runtime_profile_id` and
`trading_operator_intents.target_profile_id` to `account_slot`, backfills their
values and their `payload` keys from the activation ledger, folds
`trading_execution_runtime_control_state` onto one row per slot, drops
`trading_execution_runtime_state.runtime_profile_id`, `credential_ready` and
`activation_ready`, and drops `trading_execution_profile_activations` and
`trading_decision_runtime`. It refuses with
`trading_folded_disposition_collisions` when two profiles on the same slot each
hold a disposition Observation for the same Signal or Command, because after the
fold those two rows are one key:

```bash
pg_dump --data-only --table=trading_execution_observations \
  --table=trading_execution_profile_activations --table=trading_decision_runtime \
  > ~/.tracefold/backups/pre-0356-trading-execution-identity-$(date +%Y%m%d).sql

# Keep the newest disposition of each colliding pair and delete the rest; the
# append-only trigger has to be lifted for that one statement, with every writer
# already stopped by the canonical migration gate.
```

It also refuses with `trading_execution_identity_unmapped` when an Observation
or Command names a profile that has no activation row, because that value has no
account slot to become.

**Two operator steps go with this revision.** Delete
`trading.execution.profile_id` from `~/.tracefold/config.yaml` — strict settings
validation refuses the retired key — and apply it with the Binance account flat:
deterministic client order ids move from `tracefold:{profile_id}:{mode}` to
`tracefold:{account_slot}:{mode}`, so an order opened under the old namespace can
no longer be reclaimed by recovery.

`20260903_0357` makes the contract the only validator. It drops the twelve
JSON-shape CHECKs on `trading_execution_observations`, `trading_trade_signals`,
`trading_operator_intents` and `trading_execution_runtime_state`, and with them
the four functions they called (`trading_execution_metadata_valid`,
`trading_execution_string_array_valid`,
`trading_execution_market_key_array_valid`, `trading_jsonb_object_size`); no
function named `trading_*` is left. What stays is what only the database can
enforce: primary keys, foreign keys, NOT NULL, the enumerated value sets, the
identity regexes, the clock inequalities and the append-only triggers. It also
drops nine columns — `trading_execution_observations.payload_digest`,
`trading_trade_signals.alpha_contract_sha256` and `evidence_sha256`,
`trading_operator_intents.confirmation_identity`, and
`trading_execution_runtime_state.singleton_ready`, `portfolio_ready`,
`control_plane_ready`, `audit_ready` and `day_start_ready` — and removes the
same keys from every stored `payload`, because the contracts forbid unknown keys
and a row that still carried one could not be read back.
`trading_cases.manifest_sha256` stays: Case idempotency is still stated with it.

**#520 PR-B carries no revision but one operator step.** Delete
`trading.control.console_write_token_file` from `~/.tracefold/config.yaml` if it
is present — strict settings validation refuses the retired key — and drop the
`trading_console_write_token` file from the Serve mount. The one Command POST now
authenticates with the bootstrap `ws_token` as a bearer header. The
`~/.tracefold/trading_console_write_token` file itself can be deleted once no
compose file references it.

```bash
pg_dump --data-only --table=trading_execution_observations \
  --table=trading_trade_signals --table=trading_operator_intents \
  --table=trading_execution_runtime_state \
  > ~/.tracefold/backups/pre-0357-trading-json-checks-$(date +%Y%m%d).sql
```

The dump is the only copy afterwards: `downgrade` refuses, and the nine columns
and their payload keys are gone from the live rows as well. No operator config
step goes with it, and the account need not be flat.

`20260904_0360` deletes the lane columns no rule reads (#537 PR-3):
`trading_cases.attempt_count`, `lease_expires_at_ms`, `supplemental_source_keys`,
`strategy_id`, `strategy_version` and `strategy_config_digest`;
`trading_candidate_gate_decisions.release_revision`, `gate_version` and
`gate_config_digest`; and `trading_trade_signals.alpha_metadata`, whose key is
also removed from every stored `payload` for the reason `0357`'s were. The
admission primary key narrows to `(source_key)`, with the rulebook backfilled
into `evidence` first, and any duplicate source key collapsing to the row every
reader already showed — `CASE_CREATED` first, then the newest evaluation. The
Runtime projection's `routes` array becomes `routes_count`.

```bash
pg_dump --data-only --table=trading_cases \
  --table=trading_candidate_gate_decisions --table=trading_trade_signals \
  --table=trading_execution_runtime_state \
  > ~/.tracefold/backups/pre-0360-trading-lane-columns-$(date +%Y%m%d).sql
```

It refuses nothing and needs no operator config step, but it changes the schema
the execution Runtime writes, so `make up` refuses to apply it while the Nautilus
container is running: `make runtime-build`, then `make runtime-down` with the
account flat, then `make up`, then `make runtime-up`.

`20260904_0361` deletes the Runtime identity ceremony (#537 PR-4):
`trading_execution_runtime_state.runtime_release`, `config_sha256`,
`runtime_revision`, `image_digest`, `credential_fingerprint` and
`lifecycle_state`, with the seven CHECK constraints that only ever constrained
them; and `trading_execution_observations.runtime_release`, whose key is also
removed from every stored `payload` for the reason `0357`'s and `0360`'s were —
`ExecutionObservationV1` forbids extra keys, so a payload that still carried it
would stop materialising, including the day-start equity fact the Runtime reads
back before it will size an entry.

```bash
pg_dump --data-only --table=trading_execution_observations \
  --table=trading_execution_runtime_state \
  > ~/.tracefold/backups/pre-0361-trading-runtime-identity-$(date +%Y%m%d).sql
```

It refuses nothing and needs no operator config step. Like `0360` it changes the
schema the execution Runtime writes, so the order is `make runtime-build`, then
`make runtime-down` with the account flat, then `make up`, then `make runtime-up`.
The account must be flat for a second reason this time: a Runtime built from this
revision derives its Nautilus instance id from `account_slot:mode` rather than
from the configuration digest, and derives protection client order ids from the
replacement generation alone, so a stop resting at the venue under an id an older
build chose is not this build's and is refused as unowned exposure.

`20260904_0362` deletes the two CHECKs that ordered a venue's clock against this
host's (#544).

`news_oi_signals_available_clock_check` asserted
`available_at_ms >= observed_at_ms AND available_at_ms >= created_at_ms`. A frame
stamped a few hundred milliseconds ahead was refused, `_store_frame` does not
classify `psycopg.errors.CheckViolation`, and the News Workers process exited on
it seven times in six hours on 2026-09-04.
`news_market_liquidations_time_order` asserted `received_at_ms >= event_at_ms`
over the same pair of clocks. It never fired, because `parse_liquidation` returned
`None` for such a frame first — a guard that existed only to keep this CHECK
quiet, and whose price was discarding a forced trade that had really happened.
That guard is deleted in the same change; leaving the CHECK would have put the
refusal straight back one layer down, as the same fatal `CheckViolation`.

The revision archives nothing and refuses nothing: no row is read, written or
revalidated, every stored row already satisfies both deleted predicates, and
`news_market_liquidations` holds no rows at all. It needs no operator config step
and no stopped writer — dropping a CHECK only widens what the table accepts, so a
writer on the old schema stays correct against the new one — and it touches no
table the execution Runtime writes, so `make up` alone applies it.

Unlike the hard cuts around it, it has a real `downgrade`, because it deletes
rules and not data. Each re-added CHECK is validated against every stored row, so
a database that has since accepted an ahead-of-host fact refuses the downgrade
rather than deleting that fact, which is the correct refusal and the reason to
roll forward instead. The walk to base still stops at `20260904_0361`, one
revision later, and rolls 0362's re-added CHECKs back with it.

`20260904_0363` recreates `news_review_task_source_v1` so the verdict is joined
to the evidence snapshot it judged (#548 PR-B.2). The view took the *newest*
snapshot per Event and then required `s.evidence_version = v.evidence_version`
against the newest model triage verdict. A member joining an existing Event
appends a snapshot but does not re-run triage, so an Event with a `v2` snapshot
and a `v1` verdict satisfied neither side and vanished from the view — with its
verdict, its delivery and its accepted review. `freeze` projects that view while
`load_case` reads the snapshot by version, so the two disagreed about the same
accepted review; #534 lost four accepted Gold cases exactly this way. The
snapshot lateral is now keyed to `v.evidence_version`, and
`(event_id, evidence_version)` is that table's primary key, so it still yields at
most one row and the view still yields at most one row per Event.

`20260905_0364` adds `workers_runtime.capabilities`, a `jsonb` object keyed by
capability name (#553 PR-3). Until that revision, `workers_runtime` could say
only whether the Workers process was alive, which was enough while every
business fault was fatal: a faulted Trading lane, an unconstructable push sender
and an unassemblable News Program all ended as `lifecycle_state = 'failed'`. That
PR stops those from killing the process, so the process now legitimately stays
`running` beside one dead capability, and this column is where it says which. It
is a defaulted column addition, compatible in both directions: a writer on the
previous revision never names it and gets `'{}'`, which reads as "this runtime
published no report" rather than as a fault. `downgrade` drops it and loses only
the current process's report, which the next start republishes.

The result is a strict superset of the old one: when the newest snapshot is the
judged one, both forms select the same row byte for byte, and only the Events the
old form dropped are added. It writes and reads no row, changes no column, index,
constraint or other object, and restates the view's `security_barrier`. It
archives nothing, refuses nothing, needs no operator config step and touches no
table the execution Runtime writes, so `make up` alone applies it. Its
`downgrade` restores the previous definition exactly: a rule changed, not a fact,
so the reversal only makes the freeze blind again to reviews whose Event has
since gained a member.

## Database development standard

1. The deployment has one non-superuser application login, `tracefold`.
   Process categories use stable `application_name` values, not database roles
   or ACL matrices.
2. Connections default to autocommit. Use a short explicit transaction only
   for an atomic write or a stated consistent snapshot; keep provider, model,
   file, broker, large-JSON, and hashing work outside it.
3. One production statement has one owner. Runtime, audit, and tests reuse that
   statement or builder instead of carrying approximate copies.
4. Bind values. Compose dynamic identifiers with `psycopg.sql`.
5. Page, claim, purge, and backfill operations have a hard limit, deterministic
   order, tie-breaker, and maximum transaction/payload budget.
6. Natural PK/UNIQUE identities plus `ON CONFLICT` or conditional writes own
   idempotency; do not implement check-then-write races.
7. Keep cross-process, cross-table, economic-state, and append-only database
   invariants. Typed application models normally own single-process payload
   shape; internal permission denial is not a business rule.
8. Use typed columns for query, join, and order keys. JSONB is for bounded
   payloads that must be versioned as a whole; do not duplicate the same truth
   in scalar columns and an unconstrained payload.
9. A new index names its production query, predicate/order, measured scale,
   and write/storage cost. Zero scans are deletion evidence only after a reset
   and a complete representative business window.
10. A migration serves an actual schema or durable-data change. The current
    role model adds no application GRANT matrix. Planned downtime prefers
    ordinary transactional DDL over compatibility or dual reads.
11. Performance claims compare the same revision, configuration, parameters,
    and workload window across application, pool, and PostgreSQL evidence.
12. Every view, trigger, function, gate, timeout, index, and projection names a
    current correctness or measured-performance owner. Otherwise remove it. A
    new database role first proves a distinct trust domain; a process name is
    not sufficient justification.
