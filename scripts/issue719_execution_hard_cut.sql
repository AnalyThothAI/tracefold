-- #719 one-time, stopped-writer execution baseline cut. Run with psql -v ON_ERROR_STOP=1
-- -v account_slot=... -v expected_connection=DEMO|LIVE|TESTNET -f this-file.
-- Before running: verify a backup, stop all execution writers and Signal publishers,
-- reconcile/resolve actual venue positions and ordinary/Algo orders, and record the
-- account/environment/image identity. SQL cannot prove venue flatness.
-- Published Signals remain Case evidence and are retired before Plans/dispositions
-- disappear, so the Runtime's anti-join cannot replay them. News/research and other
-- accounts are outside this transaction. The existing pause/halt control is retained.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '300s';
CREATE TEMP TABLE issue719_scope ON COMMIT DROP AS
SELECT :'account_slot'::text AS account_slot, :'expected_connection'::text AS connection;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM issue719_scope scope
    JOIN public.trading_execution_runtime_state runtime USING (account_slot)
    JOIN public.trading_execution_runtime_control_state control USING (account_slot)
    WHERE scope.account_slot ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'
      AND scope.connection IN ('DEMO','LIVE','TESTNET')
      AND runtime.connection = scope.connection
      AND runtime.heartbeat_at_ns <
          (extract(epoch FROM transaction_timestamp()) * 1000000000)::bigint - 30000000000
  ) THEN
    RAISE EXCEPTION 'issue719_scope_or_stopped_runtime_unverified';
  END IF;
  -- This one-time cut is reviewed against the current schema only.
  IF (SELECT version_num FROM public.alembic_version) IS DISTINCT FROM '20260928_0411' THEN
    RAISE EXCEPTION 'issue719_schema_head_mismatch';
  END IF;
  -- A late retry of an old command keeps its original expiry. Once every old
  -- intent has expired, deleting its row cannot make that retry executable.
  IF EXISTS (
    SELECT 1 FROM public.trading_operator_intents intent
    JOIN issue719_scope scope USING (account_slot)
    WHERE intent.expires_at_ns >
        (extract(epoch FROM transaction_timestamp()) * 1000000000)::bigint
  ) THEN
    RAISE EXCEPTION 'issue719_operator_intent_still_executable';
  END IF;
END $$;

-- All published signals retain their Case link, but none can reenter execution.
WITH retired AS (
  INSERT INTO public.trading_signal_retirements (signal_id, reason, retired_at_ns)
  SELECT signal.signal_id, 'execution_hard_cut',
         (extract(epoch FROM transaction_timestamp()) * 1000000000)::bigint
    FROM public.trading_trade_signals signal JOIN issue719_scope scope USING (account_slot)
  ON CONFLICT (signal_id) DO NOTHING
  RETURNING signal_id
)
SELECT 'signal_retirements_added' AS object, count(*) AS affected FROM retired;

-- The immutable-ledger guards are disabled only inside this transaction. FK guards
-- stay active; an unlisted dependency aborts instead of cascading into other domains.
ALTER TABLE public.trading_execution_observations DISABLE TRIGGER USER;
ALTER TABLE public.trading_trade_plans DISABLE TRIGGER USER;
ALTER TABLE public.trading_operator_intents DISABLE TRIGGER USER;

WITH removed AS (
  DELETE FROM public.trading_entry_validity_checks checks
   USING public.trading_trade_plans plan, issue719_scope scope
   WHERE checks.entry_id = plan.entry_id AND plan.account_slot = scope.account_slot
  RETURNING checks.check_id
)
SELECT 'entry_validity_checks' AS object, count(*) AS affected FROM removed;

WITH removed AS (
  DELETE FROM public.trading_execution_observations observation
   USING issue719_scope scope WHERE observation.account_slot = scope.account_slot
  RETURNING observation.seq
)
SELECT 'execution_observations' AS object, count(*) AS affected FROM removed;

WITH removed AS (
  DELETE FROM public.trading_trade_plans plan
   USING issue719_scope scope WHERE plan.account_slot = scope.account_slot
  RETURNING plan.entry_id
)
SELECT 'trade_plans' AS object, count(*) AS affected FROM removed;

WITH removed AS (
  DELETE FROM public.trading_operator_intents intent
   USING issue719_scope scope WHERE intent.account_slot = scope.account_slot
  RETURNING intent.command_id
)
SELECT 'operator_intents' AS object, count(*) AS affected FROM removed;

WITH removed AS (
  DELETE FROM public.trading_execution_runtime_state runtime
   USING issue719_scope scope WHERE runtime.account_slot = scope.account_slot
  RETURNING runtime.account_slot
)
SELECT 'runtime_snapshots' AS object, count(*) AS affected FROM removed;

UPDATE public.trading_execution_runtime_control_state control
   SET last_command_id = NULL
  FROM issue719_scope scope WHERE control.account_slot = scope.account_slot;

ALTER TABLE public.trading_operator_intents ENABLE TRIGGER USER;
ALTER TABLE public.trading_trade_plans ENABLE TRIGGER USER;
ALTER TABLE public.trading_execution_observations ENABLE TRIGGER USER;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.trading_trade_signals signal
    JOIN issue719_scope scope USING (account_slot)
    LEFT JOIN public.trading_signal_retirements retired USING (signal_id)
    WHERE retired.signal_id IS NULL
  ) OR EXISTS (
    SELECT 1 FROM public.trading_trade_plans plan JOIN issue719_scope scope USING (account_slot)
  ) OR EXISTS (
    SELECT 1 FROM public.trading_execution_observations observation JOIN issue719_scope scope USING (account_slot)
  ) OR EXISTS (
    SELECT 1 FROM public.trading_operator_intents intent JOIN issue719_scope scope USING (account_slot)
  ) THEN
    RAISE EXCEPTION 'issue719_execution_baseline_not_empty';
  END IF;
END $$;
COMMIT;
