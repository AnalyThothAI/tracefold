"""Buy digest selection, truthful coverage and the audited model seam (#614).

SQL population and amount/quantity alignment are checked against PostgreSQL next door. These tests
exercise the public fact/selection interface and the writer's own due-time and call-budget decisions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from tracefold.news.bus import TransientError
from tracefold.news.chain_tape.contracts import RosterMember, RosterSnapshot
from tracefold.news.chain_tape.digest import (
    DIGEST_LINES_MAX,
    DigestFact,
    DigestOutcomeRow,
    DigestPack,
    DigestWindowRows,
    DigestWindowTotals,
    LastDigest,
    TokenWindowFlow,
    build_pack,
    ground,
    template_lines,
)
from tracefold.news.chain_tape.digest_writer import WalletDigestWriter
from tracefold.news.program.chain_tape_digest import (
    CHAIN_TAPE_DIGEST_MAX_TOKENS,
    CHAIN_TAPE_DIGEST_VERSION,
    ChainTapeDigestProgram,
    DigestAnswer,
    WalletDigestSignature,
)
from tracefold.news.program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    LMCallLedger,
    RecordedLM,
    RecordedLMMiss,
    RuntimeModelIdentity,
    ScriptedLM,
    program_json_adapter,
)
from tracefold.news.wallet_contracts import DigestLine

WALLET = "0x" + "11" * 20
FSD = "0x" + "aa" * 20
WINDOW_FROM = 1_788_600_000_000
WINDOW_TO = WINDOW_FROM + 4 * 3_600_000
HANDLES = {WALLET: "0xVantaa"}


def _flow(**overrides: Any) -> TokenWindowFlow:
    return replace(
        TokenWindowFlow(
            wallet=WALLET,
            token=FSD,
            token_symbol="FSD",
            token_decimals=0,
            window_buy_usd=Decimal("1000"),
            window_buy_raw=1100,
            priced_buy_raw=100,
            buys=2,
            unpriced_buys=1,
            first_buy_at_ms=WINDOW_FROM + 1000,
            last_buy_at_ms=WINDOW_FROM + 2000,
            subsequent_sells=1,
            subsequent_sell_usd=Decimal("100"),
            history_from_ms=WINDOW_FROM - 1000,
        ),
        **overrides,
    )


def _rows(**overrides: Any) -> DigestWindowRows:
    return replace(
        DigestWindowRows(
            chain_id=4663,
            totals=DigestWindowTotals(
                buys=50,
                buy_usd=Decimal("25000"),
                buy_wallets=25,
                buy_positions=25,
                sells=30,
                sell_usd=Decimal("3000000"),
                active_wallets=26,
                transfers_out=2,
                unpriced=1,
                cards=30,
                sent_cards=29,
            ),
            flows=(_flow(),),
            outcomes=(DigestOutcomeRow("buy", "1h", "observed", 4, 3, 2, -512),),
        ),
        **overrides,
    )


def _pack(**overrides: Any) -> DigestPack:
    return build_pack(
        _rows(**overrides),
        window_from_ms=WINDOW_FROM,
        window_to_ms=WINDOW_TO,
        handles=HANDLES,
        holding_costs={(WALLET, FSD): Decimal("8")},
    )


def test_same_numbers_cannot_license_a_changed_wallet_token_or_direction() -> None:
    pack = DigestPack(WINDOW_FROM, WINDOW_TO, (DigestFact("b1", "Alice 买入 FSD $1,000"),))
    swapped: Any = DigestLine(text="Bob 卖出 DOGE $1,000", cites=("b1",))

    assert ground(pack, (swapped,)).kept == 0
    honest = ground(pack, ("b1",))
    assert honest.accepted()
    assert honest.lines == (DigestLine(text="Alice 买入 FSD $1,000", cites=("b1",)),)


def test_full_totals_do_not_follow_the_number_of_selected_buy_details() -> None:
    facts = _pack().by_id()

    assert "买入合计 50 笔" in facts["w0"].text
    assert "25 个地址、25 个钱包代币组合" in facts["w0"].text
    assert "1 / 25" in facts["w1"].text
    assert "观测卡 30 张，其中已送达 29 张" in facts["w2"].text


def test_buy_fact_identifies_token_wallet_amount_count_time_and_priced_mean() -> None:
    fact = _pack().by_id()["b1"].text

    assert WALLET in fact and FSD in fact
    assert "买入 2 笔，已计价 $1,000.00" in fact
    assert "已计价部分均价 $10，" in fact  # 1000 / 100 priced units, never 1000 / 1100.
    assert "未计价 1 笔" in fact
    assert "建仓状态未知" in fact


def test_missing_history_or_subsequent_sale_cannot_claim_a_closed_position() -> None:
    pack = _pack(flows=(_flow(history_from_ms=WINDOW_FROM, subsequent_sell_usd=Decimal("9000")),))
    text = " ".join(fact.text for fact in pack.facts)

    assert "首笔买入后卖出 1 笔" in text
    assert "剩余持仓未知" in text
    assert "观察前余额与历史连续性未知" in text
    assert "快照" in text and "净现金回收线未知" in text
    assert "清空" not in text and "清仓" not in text


def test_an_entirely_unpriced_buy_has_unknown_mean_and_remains_a_buy_fact() -> None:
    pack = _pack(flows=(_flow(window_buy_usd=Decimal(0), priced_buy_raw=0, unpriced_buys=2),))

    assert "已计价部分均价 未知" in pack.by_id()["b1"].text
    assert "未计价 2 笔" in pack.by_id()["b1"].text


def test_five_buy_details_keep_their_budget_before_outcomes_or_related_sales() -> None:
    flows = tuple(_flow(token=f"0x{index:040x}", token_symbol=f"T{index}") for index in range(12))
    pack = _pack(flows=flows)
    lines = template_lines(pack)

    assert [line.cites[0] for line in lines] == ["w0", "b1", "b2", "b3", "b4", "b5", "s1", "w1"]
    assert len(lines) == DIGEST_LINES_MAX
    assert all(line.text == pack.by_id()[line.cites[0]].text for line in lines)


def test_model_only_reorders_known_buy_ids_and_cannot_select_unrelated_text() -> None:
    pack = _pack(flows=tuple(_flow(token=f"t{i}") for i in range(12)))
    selected = ground(pack, ("w2", "o1", "b12", "b1", "b1", "not-a-fact"))

    assert (selected.kept, selected.dropped) == (2, 4)
    assert [line.cites[0] for line in selected.lines] == ["w0", "b12", "b1", "b2", "b3", "b4", "s12", "w1"]
    assert all(line.text == pack.by_id()[line.cites[0]].text for line in selected.lines)


def test_outcomes_name_the_reference_and_comparable_population() -> None:
    fact = _pack().by_id()["o1"].text

    assert "buy +1h" in fact
    assert "可比较 2 条，相对观察价中位 -5.12%" in fact
    assert "发卡" not in fact


@dataclass(slots=True)
class _Db:
    """The News database port, answering with whatever the test staged. No connection anywhere."""

    state: Any
    rows: DigestWindowRows | None = None
    written: list[str] = field(default_factory=list)
    attempted_at_ms: int = 0
    refuse_digest_write: bool = False

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        return fn(_Repos(self))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        """The admission path is proved against real PostgreSQL next door; here the write is recorded.

        What these cases are about is which windows reach a write at all and whether the model was asked
        on the way, so the transaction answers "written" without a database behind it.
        """

        self.written.append(name)
        if name == "news_chain_tape_digest" and self.refuse_digest_write:
            raise TransientError("news_chain_tape_digest_write_refused")
        return fn(_Repos(self)) if name == "news_chain_tape_digest_attempt" else True


class _Repos:
    def __init__(self, db: _Db) -> None:
        self.news = _News(db)


class _News:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def chain_tape_last_digest(self, *, since_ms: int) -> Any:
        return self._db.state

    def chain_tape_current_roster(self) -> RosterSnapshot:
        return _roster()

    def chain_tape_digest_window(self, *, from_ms: int, to_ms: int) -> DigestWindowRows:
        assert self._db.rows is not None
        return self._db.rows

    def chain_tape_mark_digest_attempt(self, *, now_ms: int) -> None:
        self._db.attempted_at_ms = int(now_ms)


class _Clock:
    """A clock the test moves, so "the next turn two seconds later" is a real second call."""

    def __init__(self, at_ms: int) -> None:
        self.at_ms = at_ms

    def __call__(self) -> int:
        return self.at_ms


class _Program:
    """A model that would answer, so a test that sees no call has proved the writer refused to make one."""

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, *, facts_json: str) -> Sequence[str]:
        self.calls += 1
        return ("b1",)


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        roster_version=3,
        taken_at_ms=WINDOW_FROM,
        members=(
            RosterMember(
                wallet=WALLET,
                handle="0xVantaa",
                followers=123_456,
                realized_pnl=510_000.0,
                closed_trades=46,
                win_rate=0.44,
                profit_factor=1.6,
                open_cost=220_000.0,
                rank_quality=1,
                rank_whale=None,
            ),
        ),
    )


def test_a_window_with_no_activity_writes_nothing_and_calls_nobody() -> None:
    """#572 §5.3's 空窗跳过. Six identical "nothing happened" cards a day is what this prevents."""

    program = _Program()
    db = _Db(state=None, rows=DigestWindowRows(chain_id=0))
    writer = WalletDigestWriter(db=db, program=program, clock=lambda: WINDOW_TO)
    errors: list[str] = []

    result = asyncio.run(writer.take_digest(roster=_roster(), errors=errors))

    assert (result.digests, result.model_called, program.calls) == (0, False, 0)
    assert errors == []


def test_a_window_that_is_not_due_yet_reads_nothing_further() -> None:
    program = _Program()
    db = _Db(state=LastDigest(window_to_ms=WINDOW_TO - 60_000, model_calls_last_day=0), rows=None)
    writer = WalletDigestWriter(db=db, program=program, interval_s=14_400, clock=lambda: WINDOW_TO)

    result = asyncio.run(writer.take_digest(roster=_roster(), errors=[]))

    assert (result.digests, program.calls) == (0, 0)


def test_a_refused_write_costs_one_model_call_per_interval_rather_than_one_per_turn() -> None:
    """The loop this bounds is the expensive one: `advance()` runs every two seconds.

    A digest the database will not accept leaves the window due, and without a durable "attempted at"
    the next turn would build the pack and call the model again -- 1,800 times an hour. The attempt is
    banked before the call, so a broken write costs exactly one attempt per interval.
    """

    program = _Program()
    db = _Db(state=None, rows=_rows(), refuse_digest_write=True)
    clock = _Clock(WINDOW_TO)
    writer = WalletDigestWriter(db=db, program=program, interval_s=14_400, clock=clock)
    errors: list[str] = []

    first = asyncio.run(writer.take_digest(roster=_roster(), errors=errors))
    db.state = LastDigest(window_to_ms=0, model_calls_last_day=0, attempted_at_ms=db.attempted_at_ms)
    clock.at_ms += 2_000
    second = asyncio.run(writer.take_digest(roster=_roster(), errors=errors))

    assert (first.digests, second.digests) == (0, 0)
    # The model was asked exactly once, on the turn that banked the attempt.
    assert program.calls == 1
    assert errors == ["db:TransientError"]


def test_a_day_at_its_call_cap_still_produces_the_digest_from_the_template() -> None:
    """The cap bounds the *model*, not the summary: the facts were computed before a call was weighed."""

    program = _Program()
    db = _Db(state=LastDigest(window_to_ms=WINDOW_FROM, model_calls_last_day=24), rows=_rows())
    writer = WalletDigestWriter(
        db=db,
        program=program,
        interval_s=14_400,
        max_calls_per_day=24,
        clock=lambda: WINDOW_TO,
    )
    errors: list[str] = []

    result = asyncio.run(writer.take_digest(roster=_roster(), errors=errors))

    # The window was due and was written; the model was never asked, because the day had nothing left.
    assert (result.digests, result.model_called, program.calls) == (1, False, 0)
    # The attempt is banked before anything expensive, and the digest is written after.
    assert db.written == ["news_chain_tape_digest_attempt", "news_chain_tape_digest"]
    assert errors == []


def _program(delegate: ScriptedLM) -> ChainTapeDigestProgram:
    return ChainTapeDigestProgram(
        AuditedConfiguredLM(
            delegate,
            structured_output="json_schema" if delegate.supports_response_schema else "json_object",
            runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
            predictor="chain_tape_digest",
            route="primary",
            model_binding="chain_tape_digest.primary",
        )
    )


_ANSWER = {"digest": {"fact_ids": ["b1"]}}


def test_the_signature_runs_through_the_audited_seam_and_returns_only_fact_ids() -> None:
    pack = _pack()
    delegate = ScriptedLM([_ANSWER])

    ids = asyncio.run(_program(delegate).summarize(facts_json=pack.as_json()))

    assert ids == ("b1",)
    assert len(delegate.requests) == 1
    assert delegate.requests[0].config.max_tokens == CHAIN_TAPE_DIGEST_MAX_TOKENS
    rendered = "\n".join(
        part.text for message in delegate.requests[0].messages for part in message.parts if hasattr(part, "text")
    )
    assert "建仓状态未知" in rendered
    assert ground(pack, ids).accepted()


def test_the_digest_signature_records_and_replays_with_no_delegate_at_all() -> None:
    pack = _pack()
    delegate = ScriptedLM([_ANSWER])
    ledger = LMCallLedger()
    lm = AuditedConfiguredLM(
        delegate,
        structured_output="json_schema" if delegate.supports_response_schema else "json_object",
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
        predictor="chain_tape_digest",
        route="primary",
        model_binding="chain_tape_digest.primary",
        ledger=ledger,
    )
    with (
        ledger.scope(LMCallContext(CHAIN_TAPE_DIGEST_VERSION, "a" * 64, "b" * 64)),
        dspy.context(adapter=program_json_adapter()),
    ):
        dspy.Predict(WalletDigestSignature)(facts_json=pack.as_json(), lm=lm)
    recordings = {
        receipt.request_sha256: receipt.recording for receipt in ledger.receipts if receipt.recording is not None
    }
    assert recordings
    replay = RecordedLM(
        recordings,
        model=delegate.model,
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
        model_binding="chain_tape_digest.primary",
    )
    response = replay(request=delegate.requests[0])
    assert '"fact_ids"' in response.text
    other = dspy.LMRequest.from_call(model=delegate.model, messages=[{"role": "user", "content": "other"}])
    with pytest.raises(RecordedLMMiss):
        replay(request=other)


def test_free_text_is_refused_by_the_model_output_contract() -> None:
    with pytest.raises(ValidationError):
        DigestAnswer.model_validate({"fact_ids": ["b1"], "lines": [{"text_zh": "Bob 卖出 FSD"}]})


def test_sell_only_windows_do_not_send_a_buy_digest() -> None:
    rows = _rows(totals=DigestWindowTotals(sells=20, sell_usd=Decimal("1000000")), flows=())
    program = _Program()
    writer = WalletDigestWriter(db=_Db(state=None, rows=rows), program=program, clock=lambda: WINDOW_TO)

    result = asyncio.run(writer.take_digest(roster=_roster(), errors=[]))

    assert result.digests == 0 and program.calls == 0


def test_worker_advance_reads_its_roster_and_only_closes_its_owned_site_client() -> None:
    class Bags:
        closed = False

        async def bags(self, handle: str) -> tuple[()]:
            return ()

        async def aclose(self) -> None:
            self.closed = True

    class SharedProgram(_Program):
        async def aclose(self) -> None:
            raise AssertionError("App owns the shared model runtime")

    bags = Bags()
    writer = WalletDigestWriter(
        db=_Db(state=None, rows=_rows()), program=SharedProgram(), bags=bags, clock=lambda: WINDOW_TO
    )

    async def run() -> None:
        assert (await writer.advance()).digests == 1
        await writer.aclose()

    asyncio.run(run())
    assert bags.closed
