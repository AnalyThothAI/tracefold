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

from tracefold.news.bus import TransientError
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
    """The template is the same evidence the model would have had to cite, and no more than eight.

    It also has to ground against its own pack: the template is what a reader gets when the model does
    not, so a template line the checker would refuse is a card nobody could publish.
    """

    pack = _pack()
    lines = template_lines(pack)
    ids = {fact.id for fact in pack.facts}
    grounded = ground(pack, lines)

    assert 0 < len(lines) <= DIGEST_LINES_MAX
    assert all(len(line.cites) == 1 and line.cites[0] in ids for line in lines)
    assert (grounded.lines, grounded.dropped) == (lines, 0)


def test_the_template_reaches_the_receipts_and_the_cost_bases_before_it_lists_cards() -> None:
    """The fallback is a summary, not the first eight facts of a pack ordered for a model.

    The pack leads with cards because a model reads all of it and a card is what a reader was already
    interrupted for. A reader of the template has had those cards; what only this card carries is the
    +1h/+4h receipt and the three cost bases, and a slice of the pack would never reach either.
    """

    cards = tuple(
        DigestCardRow("exit", f"trader{index}", "FSD", 4_000, "chain_balance", 0, None, None, "", True)
        for index in range(12)
    )
    cited = [line.cites[0] for line in template_lines(_pack(cards=cards))]

    assert cited[:3] == ["w0", "w1", "k0"]
    assert "o1" in cited
    assert "c1" in cited
    # Twelve individual card facts are in the pack and none of them displaced a section.
    assert not any(cite.startswith("k") and cite != "k0" for cite in cited)


# --- grounding ------------------------------------------------------------------------------------
def test_a_line_that_invents_a_number_is_dropped_and_the_lines_beside_it_are_kept() -> None:
    """Per line, not per answer. `$23,531.60` is in the pack; `$23,900` is not.

    All-or-nothing was the first shape of this rule and it made the model dead weight: with any per-line
    error rate at all, discarding eight good sentences over a ninth rounded figure means the card a
    reader receives is the template almost every time. A line that grounds is exactly as true whatever
    the line beside it did.
    """

    pack = _pack()
    answer = (
        DigestLine(text="0xVantaa 清仓 FSD，卖前持仓约 $23,900", cites=("k1",)),
        DigestLine(text="窗口内退出卡 1 张", cites=("k0",)),
    )

    grounded = ground(pack, answer)

    assert (grounded.kept, grounded.dropped) == (1, 1)
    assert [line.text for line in grounded.lines] == ["窗口内退出卡 1 张"]
    # One surviving line is a fragment, not a summary, so the caller renders the template instead.
    assert grounded.accepted() is False


def test_an_answer_that_mostly_grounds_is_accepted_with_its_offending_line_removed() -> None:
    pack = _pack()
    answer = (
        DigestLine(text="窗口内活跃名单地址 2 个，代币 7 个", cites=("w0",)),
        DigestLine(text="合计买入 4 笔 $13,240.50", cites=("w1",)),
        DigestLine(text="0xVantaa 清仓 FSD 100%", cites=("k1",)),
        DigestLine(text="其中已送达 9 张", cites=("k0",)),
    )

    grounded = ground(pack, answer)

    assert (grounded.kept, grounded.dropped, grounded.accepted()) == (3, 1, True)
    assert "9" not in " ".join(line.text for line in grounded.lines)


def test_a_line_that_cites_a_fact_that_does_not_exist_is_dropped() -> None:
    grounded = ground(_pack(), (DigestLine(text="退出卡 1 张", cites=("k9",)),))

    assert (grounded.lines, grounded.kept, grounded.dropped) == ((), 0, 1)


def test_a_figure_is_grounded_by_any_fact_the_line_cited_however_it_was_separated() -> None:
    """`$23,531.60` and `23531.6` are the same figure; a card's `100%` is the pack's `100%`."""

    pack = _pack()
    answer = (
        DigestLine(text="0xVantaa 清仓 FSD 100%，卖前持仓 23531.6 美元", cites=("k1",)),
        DigestLine(text="窗口内活跃名单地址 2 个，代币 7 个", cites=("w0",)),
        DigestLine(text="合计买入 4 笔 $13,240.50", cites=("w1",)),
    )

    assert ground(pack, answer).lines == answer


def test_a_sign_flip_is_a_different_figure_and_the_line_that_states_it_is_dropped() -> None:
    """Half this pack is returns and cash positions that fall either side of zero.

    The +1h median came back at `-5.12%`. A line that reports it as `+5.12%` -- or drops the sign
    entirely -- is not a rounding, it is the opposite claim, and a grammar that read them as one figure
    would let it through.
    """

    pack = _pack()
    flipped = DigestLine(text="+1h 回执中位 +5.12%", cites=("o1",))
    unsigned = DigestLine(text="+1h 回执中位 5.12%", cites=("o1",))
    honest = DigestLine(text="+1h 回执中位 -5.12%", cites=("o1",))

    assert ground(pack, (flipped,)).kept == 0
    assert ground(pack, (unsigned,)).kept == 0
    assert ground(pack, (honest,)).lines == (honest,)


def test_a_negative_dollar_figure_keeps_its_sign_through_the_currency_mark() -> None:
    """`card_format.money` writes the sign outside the mark, so the grammar has to read across it.

    A rule that started at the first digit would read `净现金 $189,000.00` and `净现金 -$189,000.00` as
    the same figure -- a $378,000 swing on the net cash recovery line, which is the one dollar figure in
    this pack that is routinely negative.
    """

    flow = _rows().flows[0]
    pack = build_pack(
        _rows(
            flows=(
                TokenWindowFlow(
                    **{
                        **_as_dict(flow),
                        # Closed out, and it never got back what it put in: $229,000 in, $40,000 out.
                        "lifetime_sell_raw": 10_000_000 * UNIT,
                        "lifetime_buy_usd": Decimal("229000"),
                        "lifetime_sell_usd": Decimal("40000"),
                    }
                ),
            )
        ),
        window_from_ms=WINDOW_FROM,
        window_to_ms=WINDOW_TO,
        handles=HANDLES,
        holding_costs={},
    )
    assert "净现金 -$189,000.00" in next(fact for fact in pack.facts if fact.id == "c1").text

    honest = DigestLine(text="0xVantaa FSD 已清空，净现金 -$189,000.00", cites=("c1",))
    dropped_sign = DigestLine(text="0xVantaa FSD 已清空，净现金 $189,000.00", cites=("c1",))
    flipped = DigestLine(text="0xVantaa FSD 已清空，净现金 +$189,000.00", cites=("c1",))

    assert ground(pack, (honest,)).lines == (honest,)
    assert ground(pack, (dropped_sign,)).kept == 0
    assert ground(pack, (flipped,)).kept == 0


def test_a_clock_grounds_nothing_and_is_required_to_ground_nothing() -> None:
    """`17:20-21:20` is a window, not a figure, and its digits mean nothing on their own.

    Left in the allowed set it would let a line citing the window fact state `21 个地址` and pass. It is
    removed from both sides, so quoting the window costs nothing and a count still has to be a count.
    """

    pack = _pack()

    assert ground(pack, (DigestLine(text="窗口 17:20–21:20，代币 7 个", cites=("w0",)),)).kept == 1
    assert ground(pack, (DigestLine(text="活跃名单地址 21 个", cites=("w0",)),)).kept == 0


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # A count in Chinese numerals is a figure no fact can be compared against.
        ("窗口内有两个活跃地址", 0),
        ("二十五笔买入", 0),
        ("共三笔卖出", 0),
        # And these are ordinary words. A rule that fired on the character alone would thin almost
        # every digest by a sentence for the sake of `一`.
        ("进一步观察这批地址", 1),
        ("两者的口径并不相同", 1),
        ("买卖口径一致", 1),
    ],
)
def test_a_chinese_numeral_is_a_figure_only_where_it_counts_something(text: str, kept: int) -> None:
    grounded = ground(_pack(), (DigestLine(text=text, cites=("w0",)),))

    assert (grounded.kept, grounded.dropped) == (kept, 1 - kept)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("后市或将继续走弱", 0),
        ("建议关注这批地址", 0),
        ("值得关注的是卖出增多", 0),
        # What a digest is for: what happened, in the window's own figures.
        ("窗口内活跃名单地址 2 个，代币 7 个", 1),
    ],
)
def test_a_line_that_forecasts_or_recommends_is_dropped(text: str, kept: int) -> None:
    """The instruction already forbids these; this is what makes it enforceable.

    No fact in the pack can license a claim about what comes next, so a line making one is ungrounded
    in exactly the way an invented number is.
    """

    grounded = ground(_pack(), (DigestLine(text=text, cites=("w0",)),))

    assert (grounded.kept, grounded.dropped) == (kept, 1 - kept)


def test_a_misspelled_handle_is_as_ungrounded_as_an_invented_number() -> None:
    """A `0x` identifier is one figure, whole. Half of an address is not the address."""

    assert ground(_pack(), (DigestLine(text="0xVantea 清仓 FSD", cites=("k1",)),)).kept == 0


def test_an_answer_longer_than_the_card_keeps_only_what_the_card_can_hold() -> None:
    pack = _pack()
    line = DigestLine(text="窗口内退出卡 1 张", cites=("k0",))

    grounded = ground(pack, (line,) * (DIGEST_LINES_MAX + 2))

    assert (grounded.kept, grounded.dropped) == (DIGEST_LINES_MAX, 2)


# --- the writer's two refusals to call ------------------------------------------------------------
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
    assert ground(pack, lines).lines == tuple(lines)


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
