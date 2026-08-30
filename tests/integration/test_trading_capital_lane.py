"""The Case lifecycle's global invariants, under arbitrary sequences and against real PostgreSQL.

`tests/trading/test_capital_lane.py` owns the ordering and the vocabulary of `CapitalLane.advance()`
against a fake database, and says in its own docstring that atomicity, concurrency and the
commit-time races are proved here. This is that module.

Two things live here that an example-based test cannot state. The first is a model: `claim_case` and
`settle_case` are two statements whose correctness is a property of *every* interleaving of workers,
leases and clocks, not of the four orders someone thought to write down, so a Hypothesis
`RuleBasedStateMachine` drives them against a real database and checks the invariants after every
step. The second is a matrix: without a promotion grant there is no runtime-authority combination
that may emit capital, and "no combination" is a claim about the whole product of the authority
dimensions rather than about the one an example picked.

Everything runs against real PostgreSQL because every invariant here is enforced by SQL — a state
predicate in an `UPDATE ... WHERE`, a partial unique index, a `FOR UPDATE` — and a fake repository
would be testing the model of the rule rather than the rule.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.postgres_test_utils import connect_postgres_test
from tests.trading_v3_fixtures import binance_binding, binance_capability
from tracefold.app.repository_session import repositories_for_connection
from tracefold.trading.admission import ADMISSION_VERSION
from tracefold.trading.catalog import (
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_venue_catalog_snapshot,
)
from tracefold.trading.contracts import (
    CURRENT_TERMINAL_STATES,
    CaseState,
    FrozenMarketContext,
    FrozenPolicyContext,
    InstrumentRef,
    OiMarketTrigger,
    OiTradeCandidate,
    TradingCaseManifest,
)
from tracefold.trading.policy import CAPITAL_POLICY

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000
LEASE_MS = 30_000


def _candidate(event_id: str, *, symbol: str = "SOL") -> OiTradeCandidate:
    """The frozen public projection of one deterministic OI verdict, as the Gate hands it to a Case."""

    return OiTradeCandidate(
        event_id=event_id,
        observed_at_ms=NOW,
        verdict_created_at_ms=NOW,
        base_symbol=symbol,
        venue="binance",
        oi_direction="rise",
        oi_change_bps=720,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
        rank_in_window=1,
        final_decision="push",
        source_rule="opening_move_with_whale_concentration",
        metric_version="oi_signal_v1",
        source_strategy_id="1019",
        source_contract_version="oi_contract_v1",
        measurement_window_ms=300_000,
        learning_epoch="bundle_00000000",
        program_version="news_oi_signal_v2",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v11",
        judgment_contract_version="news_judgment_v2",
        judgment_origin="oi",
        judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )


def _manifest(event_id: str, *, symbol: str = "SOL") -> TradingCaseManifest:
    policy = CAPITAL_POLICY
    candidate = _candidate(event_id, symbol=symbol)
    return TradingCaseManifest(
        primary_trigger=OiMarketTrigger(
            source_key=candidate.source_key,
            observed_at_ms=NOW,
            persisted_at_ms=NOW,
            venue="binance",
        ),
        contexts=FrozenPolicyContext(
            oi=candidate,
            market=FrozenMarketContext(
                mark_price=Decimal("150.00"),
                observed_at_ms=NOW,
                pre_move_bps=0,
                pre_move_lookback_ms=900_000,
            ),
        ),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_config=policy.config_snapshot,
        policy_config_digest=policy.config_digest,
        underlying_key=f"crypto:{symbol}",
        base_symbol=symbol,
        cutoff_ms=NOW,
        instrument=InstrumentRef(
            exchange_id="binance",
            binding="BINANCE_USDM",
            venue="binance.usdm",
            provider_symbol=f"{symbol}USDT",
            base_symbol=symbol,
            instrument_class="crypto",
            quote_asset="USDT",
            observed_at_ms=NOW,
        ),
        venue_catalog_snapshot_sha256=FROZEN_CATALOG.snapshot_sha256,
    )


def _admission(manifest: TradingCaseManifest) -> dict[str, Any]:
    return {
        "source_key": manifest.primary_trigger.source_key,
        "gate_version": ADMISSION_VERSION,
        "gate_config_digest": "0" * 64,
        "trigger_kind": "oi",
        "underlying_key": manifest.underlying_key,
        "source_observed_at_ms": NOW,
        "status": "CASE_CREATED",
        "stage": "freeze",
        "reason": "case_created",
        "retryable": False,
        "evidence": {},
        "case_id": None,
    }


def _catalog(symbols: tuple[str, ...]) -> VenueInstrumentCatalogSnapshotV1:
    return build_venue_catalog_snapshot(
        binding="BINANCE_USDM",
        captured_at_ms=NOW,
        stale_after_ms=86_400_000,
        instruments=tuple(
            VenueInstrumentCatalogEntryV1(
                provider_instrument_id=f"{symbol}USDT",
                provider_symbol=f"{symbol}USDT",
                venue="binance.usdm",
                canonical_asset=symbol,
                canonical_namespace="crypto",
                product_kind="linear_perpetual",
                active=True,
                settlement_asset="USDT",
                margin_asset="USDT",
                price_increment="0.01",
                size_increment="0.001",
                min_quantity="0.001",
                raw_metadata_sha256=str(index) * 64,
            )
            for index, symbol in enumerate(symbols, start=1)
        ),
    )


FROZEN_CATALOG = _catalog(("SOL", "DOGE", "ETH"))
OTHER_CATALOG = _catalog(("SOL", "DOGE"))
FROZEN_CAPABILITY = binance_capability(catalog=FROZEN_CATALOG, app_revision="authority-matrix")
FROZEN_BINDING = binance_binding(catalog=FROZEN_CATALOG, capability=FROZEN_CAPABILITY)


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    repos = repositories_for_connection(connection)
    # `trading_binding_runtime.catalog_snapshot_sha256` is a foreign key, so both the snapshot a Case
    # is frozen against and the different one a mismatch points at have to be real rows.
    repos.trading.store_venue_catalog_snapshot(snapshot=FROZEN_CATALOG, now_ms=NOW)
    repos.trading.store_venue_catalog_snapshot(snapshot=OTHER_CATALOG, now_ms=NOW)
    connection.execute(
        "UPDATE trading_binding_runtime SET account_state = 'reconciled_flat', "
        "credential_state = 'configured', credential_fingerprint = %s, account_generation = 1, "
        "catalog_state = 'ready', catalog_snapshot_sha256 = %s, catalog_captured_at_ms = %s "
        "WHERE binding = 'BINANCE_USDM'",
        (FROZEN_BINDING.credential_fingerprint, FROZEN_CATALOG.snapshot_sha256, NOW),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(FROZEN_CAPABILITY, created_at_ms=NOW)
    assert repos.trading.append_and_activate_execution_binding(FROZEN_BINDING)
    connection.commit()
    yield connection
    connection.close()


class CaseLifecycle(RuleBasedStateMachine):
    """`PENDING -> RUNNING -> terminal`, driven by real SQL under an arbitrary worker interleaving.

    The model is small on purpose: the durable state of a Case, and which worker (if any) currently
    holds it. Everything else the machine knows it asks the database for, so a divergence between the
    model and the row is a finding rather than a bookkeeping error in the model.
    """

    def __init__(self, connection: Any, observed: dict[str, int]) -> None:
        super().__init__()
        self.conn = connection
        # Shared across every example so the test can prove afterwards that the branches carrying the
        # headline assertions were actually reached. An unreachable assertion passes exactly like a
        # satisfied one, and a state machine is unusually good at hiding that.
        self.observed = observed
        self.repos = repositories_for_connection(connection)
        self.clock = NOW
        # case_id -> the run_id currently believed to hold the lease, and its expiry.
        self.holders: dict[str, tuple[str | None, int]] = {}
        self.decided: dict[str, str] = {}
        self.source_keys: set[str] = set()

    @initialize()
    def start_empty(self) -> None:
        self.conn.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
        self.conn.execute("TRUNCATE trading_candidate_gate_decisions")
        self.conn.commit()

    # ------------------------------------------------------------------ rules
    @rule(symbol=st.sampled_from(["SOL", "DOGE", "ETH"]))
    def freeze_a_case(self, symbol: str) -> None:
        """A new source freezes a Case. A repeat source, or a second live thesis, must not."""

        source_key = f"oi:{uuid.uuid4().hex}:{symbol}"
        manifest = _manifest(source_key, symbol=symbol)
        case_id = uuid.uuid4().hex
        with self.repos.transaction():
            created = self.repos.trading.create_case(
                case_id=case_id, manifest=manifest, admission=_admission(manifest), now_ms=self.clock
            )
        if created:
            self.holders[case_id] = (None, 0)
            self.source_keys.add(source_key)

    @rule()
    @precondition(lambda self: bool(self.holders))
    def claim_the_oldest_claimable_case(self) -> None:
        run_id = uuid.uuid4().hex
        with self.repos.transaction():
            row = self.repos.trading.claim_case(run_id=run_id, lease_ms=LEASE_MS, now_ms=self.clock)
        if row is None:
            return
        case_id = str(row["case_id"])
        assert case_id not in self.decided, "a terminal Case must never be claimable again"
        assert row["state"] == CaseState.RUNNING.value
        self.observed["reclaimed" if self.holders.get(case_id, (None, 0))[0] is not None else "claimed"] += 1
        self.holders[case_id] = (run_id, self.clock + LEASE_MS)

    def _undecided(self) -> list[str]:
        """Cases the model believes are still decidable, held ones first.

        Both matter. Targeting a *decided* Case makes `settle_case` correctly refuse forever, and the
        rule quietly becomes a no-op that proves nothing; targeting one nobody holds makes the
        "current holder" branch unreachable, so the assertion that only the holder may terminalise a
        Case never runs. Sorting by "has a live run" puts the interesting case first.
        """

        undecided = sorted(set(self.holders) - set(self.decided))
        return sorted(undecided, key=lambda case_id: self.holders[case_id][0] is None)

    @rule(
        state=st.sampled_from(sorted(state.value for state in CURRENT_TERMINAL_STATES)),
        use_current_holder=st.booleans(),
        pick=st.integers(min_value=0, max_value=8),
    )
    @precondition(lambda self: bool(set(self.holders) - set(self.decided)))
    def settle_a_case(self, state: str, use_current_holder: bool, pick: int) -> None:
        """Terminalise with either the run that holds the lease, or a run that used to."""

        undecided = self._undecided()
        case_id = undecided[pick % len(undecided)]
        held_by, _expiry = self.holders[case_id]
        run_id = held_by if (use_current_holder and held_by is not None) else uuid.uuid4().hex
        with self.repos.transaction():
            settled = self.repos.trading.settle_case(
                case_id=case_id,
                run_id=run_id or "",
                state=CaseState(state),
                policy_decision="long",
                policy_reason="policy_long",
                capital_disposition="blocked",
                capital_reason="credentials_unconfigured",
                now_ms=self.clock,
            )
        if settled:
            assert run_id == held_by, "only the run that holds the Case may terminalise it"
            assert case_id not in self.decided, "a terminal Case cannot be decided twice"
            self.decided[case_id] = state
            self.observed["settled"] += 1
        else:
            self.observed["refused"] += 1

    @rule()
    @precondition(lambda self: bool(self.decided))
    def try_to_redecide_a_terminal_case(self) -> None:
        """A decided Case, its own run, a second answer — and the database must refuse it.

        Deliberate rather than incidental. `settle_case` carries two predicates and they fail
        differently: `run_id` stops the wrong worker, and `state IN ('PENDING','RUNNING')` stops the
        *right* worker from answering twice — the returning worker that was inside a commit while
        something else terminalised the Case. Only a rule that re-offers an already-decided Case with
        the run that decided it can reach the second one, and a rule that only ever picks undecided
        Cases never will.
        """

        case_id = sorted(self.decided)[0]
        held_by, _expiry = self.holders[case_id]
        with self.repos.transaction():
            settled = self.repos.trading.settle_case(
                case_id=case_id,
                run_id=held_by or "",
                state=CaseState(self.decided[case_id]),
                policy_decision="long",
                policy_reason="policy_long",
                capital_disposition="blocked",
                capital_reason="credentials_unconfigured",
                now_ms=self.clock,
            )
        assert not settled, "a terminal Case cannot be decided twice, not even by the run that decided it"
        self.observed["redecide_refused"] += 1

    @rule()
    def let_every_lease_expire(self) -> None:
        """Time passes. An undecided Case whose lease lapsed becomes claimable again; a decided one does not."""

        self.clock += LEASE_MS * 2

    # ------------------------------------------------------------------ invariants
    @invariant()
    def a_terminal_case_never_changes_again(self) -> None:
        for case_id, expected in self.decided.items():
            row = self.repos.trading.case(case_id=case_id)
            assert row is not None
            assert row["state"] == expected
            assert row["decided_at_ms"] is not None
            assert row["policy_decision"] == "long"

    @invariant()
    def every_case_is_in_a_state_the_writer_may_reach(self) -> None:
        rows = self.conn.execute("SELECT state, run_id, decided_at_ms FROM trading_cases").fetchall()
        reachable = {CaseState.PENDING.value, CaseState.RUNNING.value} | {
            state.value for state in CURRENT_TERMINAL_STATES
        }
        for row in rows:
            assert row["state"] in reachable
            if row["state"] == CaseState.RUNNING.value:
                assert row["run_id"], "a RUNNING Case is held by a named run"
            if row["state"] in {state.value for state in CURRENT_TERMINAL_STATES}:
                assert row["decided_at_ms"] is not None

    @invariant()
    def one_source_key_never_freezes_two_cases(self) -> None:
        duplicates = self.conn.execute(
            "SELECT primary_source_key FROM trading_cases GROUP BY primary_source_key HAVING count(*) > 1"
        ).fetchall()
        assert duplicates == []

    @invariant()
    def at_most_one_live_thesis_per_underlying(self) -> None:
        live = self.conn.execute(
            "SELECT underlying_key FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')"
            " GROUP BY underlying_key HAVING count(*) > 1"
        ).fetchall()
        assert live == []

    @invariant()
    def no_case_lifecycle_ever_emits_an_intent(self) -> None:
        """Pre-#360 there is no path from a Case to capital, whatever order the lane ran in."""

        assert int(self.conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"]) == 0

    def teardown(self) -> None:
        self.conn.rollback()


def test_the_case_lifecycle_holds_under_any_sequence_of_workers_leases_and_clocks(conn) -> None:
    """Bounded on purpose: every step is real SQL, so the budget buys interleavings, not iterations."""

    observed = dict.fromkeys(("claimed", "reclaimed", "settled", "refused", "redecide_refused"), 0)
    run_state_machine_as_test(
        lambda: CaseLifecycle(conn, observed),
        settings=settings(
            max_examples=30,
            stateful_step_count=20,
            deadline=None,
            derandomize=True,
            database=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        ),
    )

    # Every branch the invariants depend on was exercised. Without this the module could pass while
    # `settle_case` silently refused every call — which is what it does if the rule targets a Case
    # nobody holds, and the failure looks identical to success.
    assert observed["claimed"] > 0
    assert observed["reclaimed"] > 0, "an expired lease must be reclaimable, and must have been"
    assert observed["settled"] > 0, "the holder-terminalises path must be reachable"
    assert observed["refused"] > 0, "the stale-run refusal must be reachable"
    assert observed["redecide_refused"] > 0, "the terminal-state refusal must be reachable"


# ------------------------------------------------------------------ the no-grant authority matrix

CONTROLS = ("RUNNING", "PAUSED", "CLOSE_ONLY")
CREDENTIALS = ("unconfigured", "invalid", "configured")
CATALOGS = ("ready", "stale", "missing", "error")
ACCOUNTS = ("unknown", "reconciled_flat", "exposure_present")
RUNTIMES = ("stopped", "starting", "stale", "faulted", "ready")


def _expected_capital_reason(
    *,
    control: str,
    credential_state: str,
    catalog_state: str,
    account_state: str,
    runtime_state: str,
    catalog_matches: bool,
) -> str:
    """The ladder `commit_capital_disposition` walks, restated once so the matrix has an oracle.

    Restating it is the point. A test that only asserted "some reason" would pass against a ladder
    whose branches had been reordered, and the order is the product decision: an operator pause is a
    different fact from a missing key, and a reader of the Case has to be told which one happened.
    """

    if control == "PAUSED":
        return "capital_paused"
    if control == "CLOSE_ONLY":
        return "capital_close_only"
    if account_state == "exposure_present":
        return "unexpected_exposure"
    if credential_state == "unconfigured":
        return "credentials_unconfigured"
    if credential_state == "invalid":
        return "credentials_invalid"
    if not catalog_matches:
        return "catalog_mismatch"
    if catalog_state != "ready":
        return "catalog_stale"
    if runtime_state != "ready" or account_state != "reconciled_flat":
        return "binding_unready"
    return "promotion_grant_absent"


def _authority_permutations() -> list[dict[str, Any]]:
    """Every authority tuple the schema can actually hold — not the naive cross-product.

    `trading_binding_catalog_pair_check` ties the two catalog columns together: `missing` and `error`
    require no snapshot at all, `ready` and `stale` require one. Generating the free product would
    spend most of the matrix on rows PostgreSQL refuses to store, and would say nothing about the
    ladder. A binding with no snapshot is a mismatch by construction, because NULL is not the sha the
    Case was frozen against.
    """

    return [
        {
            "control": control,
            "credential_state": credential_state,
            "catalog_state": catalog_state,
            "account_state": account_state,
            "runtime_state": runtime_state,
            "catalog_matches": catalog_matches,
        }
        for control in CONTROLS
        for credential_state in CREDENTIALS
        for account_state in ACCOUNTS
        for runtime_state in RUNTIMES
        for catalog_state in CATALOGS
        # `missing` and `error` carry no snapshot at all, so they can only be a mismatch.
        for catalog_matches in ((True, False) if catalog_state in {"ready", "stale"} else (False,))
    ]


def _apply_authority(conn: Any, manifest: TradingCaseManifest, authority: dict[str, Any]) -> None:
    stores_a_snapshot = authority["catalog_state"] in {"ready", "stale"}
    snapshot_sha = (
        (manifest.venue_catalog_snapshot_sha256 if authority["catalog_matches"] else OTHER_CATALOG.snapshot_sha256)
        if stores_a_snapshot
        else None
    )
    conn.execute("UPDATE trading_runtime_state SET control = %s WHERE id = 1", (authority["control"],))
    conn.execute(
        """
        UPDATE trading_binding_runtime
           SET credential_state = %s, credential_fingerprint = %s, runtime_state = %s,
               account_state = %s, catalog_state = %s, catalog_snapshot_sha256 = %s,
               catalog_captured_at_ms = %s, updated_at_ms = %s
         WHERE binding = 'BINANCE_USDM'
        """,
        (
            authority["credential_state"],
            FROZEN_BINDING.credential_fingerprint if authority["credential_state"] == "configured" else None,
            authority["runtime_state"],
            authority["account_state"],
            authority["catalog_state"],
            snapshot_sha,
            NOW if stores_a_snapshot else None,
            NOW,
        ),
    )


def _walk_the_authority_matrix(
    conn: Any,
    repos: Any,
    permutations: list[dict[str, Any]],
    reasons_seen: set[str],
    mismatches: list[str],
) -> None:
    """One frozen Case per authority tuple, decided, and compared against the ladder's own oracle."""

    for authority in permutations:
        manifest = _manifest(uuid.uuid4().hex)
        _apply_authority(conn, manifest, authority)
        case_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        with repos.transaction():
            assert repos.trading.create_case(
                case_id=case_id, manifest=manifest, admission=_admission(manifest), now_ms=NOW
            )
        with repos.transaction():
            claimed = repos.trading.claim_case(run_id=run_id, lease_ms=LEASE_MS, now_ms=NOW)
        assert claimed is not None and str(claimed["case_id"]) == case_id

        with repos.transaction():
            commit = repos.trading.commit_capital_disposition(
                case_id=case_id,
                run_id=run_id,
                manifest=manifest,
                policy_reason="policy_long",
                policy_checks={"decision": "long"},
                release_revision="test-release",
                source_contract_sha256="1" * 64,
                feature_contract_sha256="2" * 64,
                target_notional=Decimal("7.5"),
                now_ms=NOW,
            )
        conn.commit()

        expected = _expected_capital_reason(**authority)
        reasons_seen.add(commit.reason)
        row = repos.trading.case(case_id=case_id)
        actual = {
            "state": None if row is None else row["state"],
            "policy_decision": None if row is None else row["policy_decision"],
            "capital_disposition": None if row is None else row["capital_disposition"],
            "capital_reason": None if row is None else row["capital_reason"],
            "commit_state": commit.state,
            "commit_reason": commit.reason,
        }
        wanted = {
            "state": CaseState.BLOCKED.value,
            "policy_decision": "long",
            "capital_disposition": "blocked",
            "capital_reason": expected,
            "commit_state": CaseState.BLOCKED,
            "commit_reason": expected,
        }
        if actual != wanted:
            mismatches.append(f"{authority} -> {actual} (wanted {wanted})")


def test_no_runtime_authority_combination_emits_capital_without_promotion_grant(conn) -> None:
    """Every point in the no-grant authority product reaches BLOCKED with one exact reason.

    No branch may emit capital without a promotion grant — not even a fully configured, ready,
    reconciled runtime, which is the permutation an example-based test is least likely to include
    and the only one that can prove the final `promotion_grant_absent` rung is reachable.

    One test rather than one per tuple: the work is a handful of statements each, and the per-case
    reporting overhead of eight hundred parametrizations is an order of magnitude more than the
    behaviour being measured. Every mismatch is collected, so a reordered ladder shows its whole
    shape at once instead of one tuple at a time.
    """

    repos = repositories_for_connection(conn)
    permutations = _authority_permutations()
    reasons_seen: set[str] = set()
    mismatches: list[str] = []
    conn.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    conn.execute("TRUNCATE trading_candidate_gate_decisions")
    # Eight hundred round trips through the ladder is eight hundred commits, and this test is about
    # what the ladder decides rather than about surviving a power cut mid-matrix. Visibility, locking
    # and constraint semantics — everything the assertions read — are unchanged.
    conn.execute("SET synchronous_commit = off")
    conn.commit()

    # No truncation between permutations, deliberately. Each iteration freezes a Case under a fresh
    # source key, and the previous one is already terminal, so the uniqueness index on a *live*
    # thesis is satisfied without emptying the table eight hundred times.
    try:
        _walk_the_authority_matrix(conn, repos, permutations, reasons_seen, mismatches)
    finally:
        conn.execute("SET synchronous_commit = on")
        conn.commit()

    assert mismatches == []
    assert int(conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"]) == 0
    assert int(conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"]) == len(permutations)
    assert len(permutations) == 3 * 3 * 3 * 5 * 6
    # Every rung of the ladder is reachable, including the last one: a runtime with nothing wrong
    # with it still emits no capital, which is the whole no-grant contract.
    assert reasons_seen == {
        "capital_paused",
        "capital_close_only",
        "credentials_unconfigured",
        "credentials_invalid",
        "catalog_mismatch",
        "catalog_stale",
        "unexpected_exposure",
        "binding_unready",
        "promotion_grant_absent",
    }
