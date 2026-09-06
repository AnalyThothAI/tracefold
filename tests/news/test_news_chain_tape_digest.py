"""The wallet digest without a database and without a network (#572 PR-3).

Three things are proved here, and they are the three the digest's whole design rests on:

* the fact pack states every figure a reader can be shown, computed from stored sums alone -- including
  the three cost bases #572 §5.3 insists on naming separately;
* a model answer that states a figure the facts it cited do not carry is dropped *whole*, and the
  deterministic template is what a reader gets;
* the two conditions that stop a call happening at all -- an empty window and a spent day -- are the
  writer's, not the model's.

The one Signature is exercised through the same audited seam production uses, against a scripted
delegate and then against the recording that delegate produced. No network, either way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.chain_tape.contracts import RosterMember, RosterSnapshot
from tracefold.news.chain_tape.digest import (
    DIGEST_LINES_MAX,
    DigestCardRow,
    DigestOutcomeRow,
    DigestPack,
    DigestWindowRows,
    LastDigest,
    TokenWindowFlow,
    WalletWindowActivity,
    build_pack,
    ground,
    template_lines,
)
from tracefold.news.chain_tape.digest_writer import WalletDigestWriter
from tracefold.news.program.chain_tape_digest import (
    CHAIN_TAPE_DIGEST_MAX_TOKENS,
    CHAIN_TAPE_DIGEST_VERSION,
    ChainTapeDigestProgram,
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
PEER = "0x" + "22" * 20
FSD = "0x" + "aa" * 20
UNIT = 10**18
WINDOW_FROM = 1_788_600_000_000
WINDOW_TO = WINDOW_FROM + 4 * 3_600_000
HANDLES = {WALLET: "0xVantaa", PEER: "smol_intern"}


def _rows(**overrides: Any) -> DigestWindowRows:
    base: dict[str, Any] = {
        "chain_id": 4663,
        "activity": (
            WalletWindowActivity(WALLET, 3, Decimal("12340.50"), 2, Decimal("23531.60"), 1, 0),
            WalletWindowActivity(PEER, 1, Decimal("900"), 0, Decimal(0), 0, 1),
        ),
        "flows": (
            TokenWindowFlow(
                wallet=WALLET,
                token=FSD,
                token_symbol="FSD",
                token_decimals=18,
                window_buy_usd=Decimal("12340.50"),
                window_buy_raw=4_000_000 * UNIT,
                window_sell_usd=Decimal("23531.60"),
                lifetime_buy_usd=Decimal("40000"),
                lifetime_sell_usd=Decimal("23531.60"),
                lifetime_buy_raw=10_000_000 * UNIT,
                lifetime_sell_raw=6_000_000 * UNIT,
                lifetime_out_raw=0,
            ),
        ),
        "cards": (
            DigestCardRow("exit", "0xVantaa", "FSD", 10_000, "chain_balance", 0, None, Decimal("23531.60"), "", True),
        ),
        "outcomes": (DigestOutcomeRow("1h", 4, 3, -512),),
        "tokens": 7,
        "unpriced": 2,
    }
    base.update(overrides)
    return DigestWindowRows(**base)


def _pack(**overrides: Any) -> DigestPack:
    return build_pack(
        _rows(**overrides),
        window_from_ms=WINDOW_FROM,
        window_to_ms=WINDOW_TO,
        handles=HANDLES,
        holding_costs={(WALLET, FSD): Decimal("0.0018")},
    )


# --- the fact pack --------------------------------------------------------------------------------
def test_the_pack_names_the_three_cost_bases_separately_and_computes_each_from_its_own_sums() -> None:
    """#572 §5.3: three questions, three numbers, and never an average of them.

    The window entry price is this window's dollars over this window's quantity ($12,340.50 / 4M). The
    holding cost is the provider's own moving average and is nobody's arithmetic. The recovery line is
    net cash still out over what is still held ((40,000 - 23,531.60) / 4M), which is a different
    number from both and can be above or below either.
    """

    costs = next(fact for fact in _pack().facts if fact.id == "c1")

    assert "观察期买入均价 $0.003085" in costs.text
    assert "剩余持仓成本 $0.0018" in costs.text
    assert "净现金回收线 $0.004117" in costs.text


def test_a_cost_basis_the_provider_did_not_answer_is_stated_as_unknown_rather_than_dropped() -> None:
    """A missing line reads as "no such position", which is a different claim from "we do not know"."""

    pack = build_pack(
        _rows(),
        window_from_ms=WINDOW_FROM,
        window_to_ms=WINDOW_TO,
        handles=HANDLES,
        holding_costs={},
    )

    assert "剩余持仓成本 未知" in next(fact for fact in pack.facts if fact.id == "c1").text


def test_a_recovery_line_below_zero_is_stated_as_the_negative_number_it_is() -> None:
    """A wallet that has already taken out more than it put in owes nothing back, and the line says so.

    (40,000 - 60,000) / 4M held = -$0.005. Not clamped and not hidden: #572 §5.3 says in as many words
    that this figure can be negative, and a floor at zero would state a position that does not exist.
    """

    flow = _rows().flows[0]
    pack = build_pack(
        _rows(flows=(TokenWindowFlow(**{**_as_dict(flow), "lifetime_sell_usd": Decimal("60000")}),)),
        window_from_ms=WINDOW_FROM,
        window_to_ms=WINDOW_TO,
        handles=HANDLES,
        holding_costs={},
    )

    assert "净现金回收线 -$0.005" in next(fact for fact in pack.facts if fact.id == "c1").text


def test_a_position_with_nothing_left_states_its_net_cash_instead_of_a_recovery_line() -> None:
    """There is no denominator to divide by, so the fact answers the question that is still open."""

    flow = _rows().flows[0]
    pack = build_pack(
        _rows(flows=(TokenWindowFlow(**{**_as_dict(flow), "lifetime_sell_raw": 10_000_000 * UNIT}),)),
        window_from_ms=WINDOW_FROM,
        window_to_ms=WINDOW_TO,
        handles=HANDLES,
        holding_costs={},
    )

    assert "净现金回收线 已清空，净现金 -$16,468.40" in next(fact for fact in pack.facts if fact.id == "c1").text


def test_every_template_line_cites_the_one_fact_it_was_rendered_from() -> None:
    """The template is the same evidence the model would have had to cite, and no more than eight."""

    pack = _pack()
    lines = template_lines(pack)
    ids = {fact.id for fact in pack.facts}

    assert 0 < len(lines) <= DIGEST_LINES_MAX
    assert all(len(line.cites) == 1 and line.cites[0] in ids for line in lines)
    assert ground(pack, lines) == lines


# --- grounding ------------------------------------------------------------------------------------
def test_a_line_that_invents_a_number_drops_the_whole_digest() -> None:
    """One hallucinated figure and the reader gets the template: the other lines are not trustworthy.

    `$23,531.60` is in the pack; `$23,900` is not, and no fact the line cites carries it.
    """

    pack = _pack()
    answer = (
        DigestLine(text="0xVantaa 清仓 FSD，卖前持仓约 $23,900", cites=("k1",)),
        DigestLine(text="窗口内退出卡 1 张", cites=("k0",)),
    )

    assert ground(pack, answer) is None


def test_a_line_that_cites_a_fact_that_does_not_exist_drops_the_whole_digest() -> None:
    pack = _pack()

    assert ground(pack, (DigestLine(text="退出卡 1 张", cites=("k9",)),)) is None


def test_a_figure_is_grounded_by_any_fact_the_line_cited_however_it_was_separated() -> None:
    """`$23,531.60` and `23531.6` are the same figure; a card's `100%` is the pack's `100%`."""

    pack = _pack()
    answer = (
        DigestLine(text="0xVantaa 清仓 FSD 100%，卖前持仓 23531.6 美元", cites=("k1",)),
        DigestLine(text="窗口内活跃名单地址 2 个，代币 7 个", cites=("w0",)),
    )

    assert ground(pack, answer) == answer


def test_a_misspelled_handle_is_as_ungrounded_as_an_invented_number() -> None:
    """A `0x` identifier is one figure, whole. Half of an address is not the address."""

    assert ground(_pack(), (DigestLine(text="0xVantea 清仓 FSD", cites=("k1",)),)) is None


def test_an_answer_longer_than_the_card_is_refused_outright() -> None:
    pack = _pack()
    line = DigestLine(text="窗口内退出卡 1 张", cites=("k0",))

    assert ground(pack, (line,) * (DIGEST_LINES_MAX + 1)) is None


# --- the writer's two refusals to call ------------------------------------------------------------
@dataclass(slots=True)
class _Db:
    """The News database port, answering with whatever the test staged. No connection anywhere."""

    state: Any
    rows: DigestWindowRows | None = None
    written: list[str] = field(default_factory=list)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        return fn(_Repos(self))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        """The admission path is proved against real PostgreSQL next door; here the write is recorded.

        What these cases are about is which windows reach a write at all and whether the model was asked
        on the way, so the transaction answers "written" without a database behind it.
        """

        self.written.append(name)
        return True


class _Repos:
    def __init__(self, db: _Db) -> None:
        self.news = _News(db)


class _News:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def chain_tape_last_digest(self, *, since_ms: int) -> Any:
        return self._db.state

    def chain_tape_digest_window(self, *, from_ms: int, to_ms: int) -> DigestWindowRows:
        assert self._db.rows is not None
        return self._db.rows


class _Program:
    """A model that would answer, so a test that sees no call has proved the writer refused to make one."""

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, *, facts_json: str) -> Sequence[DigestLine]:
        self.calls += 1
        return (DigestLine(text="窗口内退出卡 1 张", cites=("k0",)),)


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
    assert db.written == ["news_chain_tape_digest"]
    assert errors == []


# --- the one Signature, through the audited seam --------------------------------------------------
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


_ANSWER = {
    "digest": {
        "lines": [
            {"text_zh": "0xVantaa 清仓 FSD 100%", "cites": ["k1"]},
            {"text_zh": "窗口内退出卡 1 张、拥挤卡 0 张", "cites": ["k0"]},
        ]
    }
}


def test_the_signature_runs_through_the_audited_seam_and_its_answer_grounds() -> None:
    """One call, the pack in the prompt, and typed lines out that the pack itself accepts."""

    pack = _pack()
    delegate = ScriptedLM([_ANSWER])

    lines = asyncio.run(_program(delegate).summarize(facts_json=pack.as_json()))

    assert len(delegate.requests) == 1
    assert delegate.requests[0].config.max_tokens == CHAIN_TAPE_DIGEST_MAX_TOKENS
    rendered = "\n".join(
        part.text for message in delegate.requests[0].messages for part in message.parts if hasattr(part, "text")
    )
    assert "0xVantaa 清仓 FSD 100%" in rendered
    assert ground(pack, lines) == lines


def test_the_digest_signature_records_and_replays_with_no_delegate_at_all() -> None:
    """The recorded-LM path #572 §5.4 asks for: this Signature's own call, replayed by request identity.

    The Predictor is driven directly rather than through `ChainTapeDigestProgram` for one reason: the
    Program opens its own ledger scope, and a recording is a receipt on a ledger the caller holds. What
    is proved is the same thing either way -- the request this Signature renders and the answer it
    parses survive a round trip through the seam's own recording, and a request the recording does not
    address is a miss rather than a live call.
    """

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

    assert '"text_zh"' in response.text
    other = dspy.LMRequest.from_call(model=delegate.model, messages=[{"role": "user", "content": "other"}])
    with pytest.raises(RecordedLMMiss):
        replay(request=other)


def test_an_answer_that_is_not_chinese_is_refused_by_the_output_contract() -> None:
    """A digest is Chinese reader copy. An English passthrough is a rejected output, not a card."""

    delegate = ScriptedLM([{"digest": {"lines": [{"text_zh": "exit card sent", "cites": ["k0"]}]}}] * 2)

    with pytest.raises(Exception, match=r"chain_tape_digest_line_not_chinese|adapter"):
        asyncio.run(_program(delegate).summarize(facts_json=_pack().as_json()))


def _as_dict(flow: TokenWindowFlow) -> dict[str, Any]:
    return {field: getattr(flow, field) for field in flow.__slots__}
