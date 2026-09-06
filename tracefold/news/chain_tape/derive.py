"""Turning stored fills into observations a reader receives, and cards into price receipts (#572 PR-2).

This is the half of the tape that PR-1 deliberately did not write. It runs inside the same
`ChainTapeLoop.advance()` turn, immediately after the fills of that turn are committed, and it is a
straight line: verify the sells, ask `rules` whether anything is worth a card, gather the provider's own
context for the ones that are, and open one ordinary market Item per card through the same
`admit_market_item` transaction OI, liquidation and smart money go through.

Three deliberate positions, all of them from #572's 2026-09-06 decision to close the loop rather than
wait a calibration week:

* **Freshness is the only suppression that matters here.** A fill whose block time is more than
  `trigger_max_age_s` behind the moment this host read it is history -- which is exactly what the 24-hour
  backfill is. It gives the cascade and crowding rules their context and can never send a card.
* **Verification degrades, it does not block.** The chain's `balanceOf` at the block before the sell is
  the denominator where the public node still holds that state; the provider's own bag plus the amount
  just sold is the denominator where it does not; and where neither answers, the sale itself is the only
  position anything can see. The card says which, in one short label, and goes out either way.
* **Every external answer is optional.** A mark, a bag, a pool depth, a DexScreener price -- each one
  costs its own line and nothing else. Nothing here waits on a provider to decide whether a reader is
  told something the chain already proved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final, Protocol

from ..pipeline.admission import admit_market_item, prepare_wallet_observation, wallet_item_id
from ..telemetry import NewsExternalDataSource, NewsExternalDataTelemetryPort
from ..wallet_contracts import (
    OUTCOME_PRICE_MIN,
    OUTCOME_UNAVAILABLE,
    WalletBalance,
    WalletCheck,
    WalletEvent,
    WalletOutcome,
)
from .contracts import CHAIN_TAPE_NAME, ClassifiedFill, RosterMember, RosterSnapshot
from .rules import CrowdingCard, ExitCard, WalletRules, decide_crowding, decide_exit, is_live, ratio_bps
from .tape_io import FAILED, TapePasses, bag_for, tape_decimal

CHAIN_SOURCE: Final[NewsExternalDataSource] = "robinhood_rpc"
SITE_SOURCE: Final[NewsExternalDataSource] = "robinhoodtrenches"
PRICE_SOURCE: Final[NewsExternalDataSource] = "dexscreener"

# How many price receipts one turn may take, split evenly across the horizons so a backlog on one can
# never starve the other. The horizons are an hour and four hours apart, so a handful per turn drains
# any backlog in minutes while keeping ingestion the busy path.
OUTCOMES_PER_TURN_MAX: Final = 4

_DB_READ_TIMEOUT_SECONDS: Final = 5.0
_DB_WRITE_TIMEOUT_SECONDS: Final = 10.0

# What the provider's own price feed is called on a receipt row, when DexScreener could not answer.
SITE_PRICE_SOURCE: Final = "robinhoodtrenches_mark"


class BalancePort(Protocol):
    """The one state read the exit rule needs, and the chain id every row is stamped with."""

    @property
    def chain_id(self) -> int: ...

    async def balance_of(self, token: str, wallet: str, *, block_number: int) -> int | None: ...


class SiteContextPort(Protocol):
    """The provider's own figures: what a wallet holds and paid, and what a token is worth now."""

    async def bags(self, handle: str) -> Sequence[Any]: ...

    async def marks(self) -> Mapping[str, Any]: ...


class PricePort(Protocol):
    """One token price for a receipt. `None` means "not indexed", which is an answer."""

    async def token_price(self, address: str) -> Decimal | None: ...


@dataclass(frozen=True, slots=True)
class DeriveResult:
    """What one derivation pass did, counted rather than logged, so a test can assert on a turn."""

    checks: int = 0
    exits: int = 0
    crowding: int = 0
    outcomes: int = 0
    unavailable: int = 0


@dataclass(slots=True)
class _Plan:
    """One turn's writes, gathered outside any transaction and committed in one."""

    checks: list[WalletCheck] = field(default_factory=list)
    events: list[WalletEvent] = field(default_factory=list)


class WalletCardDeriver(TapePasses):
    """The rules half of the tape: fills in, observations and receipts out.

    Its own object rather than more methods on the loop, because it has its own ports (the site's context
    endpoints, a price feed) and its own bounded turn. The loop owns when it runs; this owns what it does.
    """

    _read_timeout_seconds = _DB_READ_TIMEOUT_SECONDS
    _write_timeout_seconds = _DB_WRITE_TIMEOUT_SECONDS
    _failure_stage = "derive"
    _failure_label = "derivation"

    def __init__(
        self,
        *,
        db: Any,
        chain: BalancePort,
        site: SiteContextPort,
        prices: PricePort | None = None,
        rules: WalletRules | None = None,
        telemetry: NewsExternalDataTelemetryPort | None = None,
        clock: Callable[[], int],
    ) -> None:
        self.db = db
        self.chain = chain
        self.site = site
        self.prices = prices
        self.rules = rules or WalletRules()
        self.telemetry = telemetry
        self._clock = clock

    # ------------------------------------------------------------------ the pass
    async def derive(
        self,
        fills: Sequence[ClassifiedFill],
        *,
        roster: RosterSnapshot,
        errors: list[str],
    ) -> DeriveResult:
        """Read this turn's fills as observations. Every failure here is a missing line, never a fault."""

        live = tuple(
            fill
            for fill in fills
            if is_live(event_at_ms=fill.event_at_ms, received_at_ms=fill.received_at_ms, rules=self.rules)
        )
        if not live:
            return DeriveResult()
        members = {member.wallet: member for member in roster.members}
        marks = await self._marks(errors)
        plan = _Plan()
        # Every live fill of this turn, and there is no cap here on purpose. The ingestion half already
        # bounds the turn -- at most `receipts_per_turn_max` transactions are classified, and only the
        # movements of the last few minutes are live -- so a second cap here would silently drop the
        # newest movements of a busy turn and never look at them again: the fills are already classified,
        # so the next turn's `fills` does not re-offer them.
        for fill in live:
            if fill.kind == "sell" and fill.wallet in members:
                await self._sell(fill, member=members[fill.wallet], marks=marks, plan=plan, errors=errors)
        seen_tokens: set[str] = set()
        for fill in live:
            if fill.kind != "buy" or fill.token in seen_tokens:
                continue
            seen_tokens.add(fill.token)
            await self._crowding(fill, roster=roster, members=members, marks=marks, plan=plan, errors=errors)
        if not plan.checks and not plan.events:
            return DeriveResult()
        if not await self._commit(plan, errors):
            # Nothing committed, so nothing is reported. The counters on the tape's state row are what
            # an operator reads as "this turn opened these cards"; a planned row that was rolled back
            # opened nothing.
            return DeriveResult()
        return DeriveResult(
            checks=len(plan.checks),
            exits=sum(1 for event in plan.events if event.kind == "exit"),
            crowding=sum(1 for event in plan.events if event.kind == "crowding"),
        )

    # ------------------------------------------------------------------ exit
    async def _sell(
        self,
        fill: ClassifiedFill,
        *,
        member: RosterMember,
        marks: Mapping[str, Any],
        plan: _Plan,
        errors: list[str],
    ) -> None:
        bags = await self._bags(member.handle, errors)
        balance = await self._balance(fill, bags=bags, errors=errors)
        if balance is None:
            # Neither authority answered, so there is no denominator and nothing honest to say about
            # this sell. The fill stays classified and nothing else happens: a check row would have to
            # name a basis it does not have, and a card would be an exit ratio invented out of a
            # provider outage.
            return
        share = ratio_bps(balance_before_raw=balance.q_before_raw, quantity_raw=fill.amount_raw)
        plan.checks.append(
            WalletCheck(
                chain_id=fill.chain_id,
                tx_hash=fill.tx_hash,
                log_index=fill.log_index,
                basis=balance.basis,
                q_before_raw=balance.q_before_raw,
                q_sell_raw=fill.amount_raw,
                ratio_bps=share,
                block_hash=balance.block_hash,
                checked_at_ms=self._clock(),
                error=balance.error,
            )
        )
        mark = _mark(marks.get(fill.token)) or _implied_price(fill)
        position_usd = _position_value(balance.q_before_raw, fill.token_decimals, mark)
        # `bags` is not None past the guard above: `_balance` returns None exactly when the site did
        # not answer, and that case has already returned.
        bag = bag_for(bags or (), token=fill.token, symbol=fill.token_symbol)
        if position_usd is None and bag is not None:
            # No price anywhere. What the wallet paid is still a size, and it is the provider's own
            # number rather than one this process invented.
            position_usd = tape_decimal(bag.cost_usd)
        window_from = fill.event_at_ms - self.rules.exit_cascade_window_ms
        context = await self._read(
            "news_chain_tape_exit_context",
            lambda repos: (
                repos.news.chain_tape_cascade_buys(
                    chain_id=fill.chain_id,
                    token=fill.token,
                    exclude_wallet=fill.wallet,
                    from_ms=window_from,
                    to_ms=fill.event_at_ms,
                ),
                repos.news.chain_tape_last_exit(chain_id=fill.chain_id, wallet=fill.wallet, token=fill.token),
                repos.news.chain_tape_recent_crowding_item(
                    chain_id=fill.chain_id, token=fill.token, since_ms=window_from
                ),
            ),
            errors,
        )
        if context is FAILED:
            return
        (cascade_wallets, cascade_usd), previous, crowding_item = context
        card = decide_exit(
            balance=balance,
            quantity_raw=fill.amount_raw,
            position_usd=position_usd,
            cascade_wallets=cascade_wallets,
            cascade_usd=cascade_usd,
            event_at_ms=fill.event_at_ms,
            previous=previous,
            rules=self.rules,
        )
        if card is None:
            return
        plan.events.append(
            _exit_event(
                fill,
                member=member,
                card=card,
                mark=mark,
                entry_price=None if bag is None else tape_decimal(bag.avg_price),
                crowding_item_id=crowding_item,
            )
        )

    async def _balance(
        self, fill: ClassifiedFill, *, bags: Sequence[Any] | None, errors: list[str]
    ) -> WalletBalance | None:
        """The denominator, from the chain where it can be had and from the provider where it cannot.

        Three tiers, and the third one is the reason `bags` may be `None`. `balanceOf` at the block
        before the sell is the chain's own answer while the public node still holds that state. Past
        that window the provider's reported bag plus the amount just sold reconstructs the same
        quantity, and the card says it was reconstructed. And when the provider says the wallet holds
        *none* of this token, the position was the sale: a genuine full exit.

        `None` is the fourth case and it is not a tier: the chain did not answer and neither did the
        site, so nobody knows what was held. Treating that as a full exit is what a rate-limited RPC
        during a 20% sell would turn into a `清仓` card, which is why "the site answered with no bag"
        and "the site did not answer" cannot be the same value.
        """

        answer = await self._call(
            CHAIN_SOURCE,
            lambda: self.chain.balance_of(fill.token, fill.wallet, block_number=max(0, fill.block_number - 1)),
            errors,
        )
        if answer is not FAILED and isinstance(answer, int):
            return WalletBalance(q_before_raw=int(answer), basis="chain_balance", block_hash=fill.block_hash)
        if bags is None:
            return None
        reason = "rpc_state_unavailable" if answer is not FAILED else "rpc_call_failed"
        bag = bag_for(bags, token=fill.token, symbol=fill.token_symbol)
        remaining = _raw_amount(None if bag is None else bag.amount, fill.token_decimals)
        if remaining is not None:
            # What the provider says is left, plus what just left: the position as it stood before this
            # sell, reconstructed. It is arithmetic on somebody else's number, and the card says so.
            return WalletBalance(
                q_before_raw=remaining + int(fill.amount_raw),
                basis="site_reported",
                block_hash=fill.block_hash,
                error=reason,
            )
        # The provider answered and holds no position in this token, so the sale *was* the position.
        return WalletBalance(
            q_before_raw=int(fill.amount_raw),
            basis="site_reported",
            block_hash=fill.block_hash,
            error=f"{reason}:no_reported_bag",
        )

    # ------------------------------------------------------------------ crowding
    async def _crowding(
        self,
        fill: ClassifiedFill,
        *,
        roster: RosterSnapshot,
        members: Mapping[str, RosterMember],
        marks: Mapping[str, Any],
        plan: _Plan,
        errors: list[str],
    ) -> None:
        window_from = fill.event_at_ms - self.rules.crowding_window_ms
        context = await self._read(
            "news_chain_tape_crowding_context",
            lambda repos: (
                repos.news.chain_tape_crowding_buyers(
                    chain_id=fill.chain_id, token=fill.token, from_ms=window_from, to_ms=fill.event_at_ms
                ),
                repos.news.chain_tape_last_crowding(chain_id=fill.chain_id, token=fill.token),
            ),
            errors,
        )
        if context is FAILED:
            return
        buyers, previous = context
        card = decide_crowding(buyers=tuple(buyers), window_from_ms=window_from, previous=previous, rules=self.rules)
        if card is None:
            return
        lead = members.get(card.lead.wallet)
        plan.events.append(
            _crowding_event(
                fill,
                card=card,
                lead=lead,
                roster_version=roster.roster_version,
                followers=sum(int(members[buyer.wallet].followers) for buyer in card.buyers if buyer.wallet in members),
                liquidity=_liquidity(marks.get(fill.token)),
            )
        )

    # ------------------------------------------------------------------ price receipts
    async def take_outcomes(self, errors: list[str]) -> DeriveResult:
        """Fill in the +1h and +4h prices for cards already sent. Bounded, and never on the card path."""

        stamp = self._clock()
        due = await self._read(
            "news_chain_tape_outcomes_due",
            lambda repos: repos.news.chain_tape_due_outcomes(now_ms=stamp, limit=OUTCOMES_PER_TURN_MAX),
            errors,
        )
        if due is FAILED or not due:
            return DeriveResult()
        marks: Mapping[str, Any] | None = None
        written: list[WalletOutcome] = []
        for row in due:
            price = await self._price(str(row["token"]), errors)
            source = "dexscreener"
            if price is None:
                if marks is None:
                    marks = await self._marks(errors)
                price = _mark(marks.get(str(row["token"])))
                source = SITE_PRICE_SOURCE
            if price is not None and price < OUTCOME_PRICE_MIN:
                # A price of zero is not a price, and neither is one the receipt column cannot hold:
                # `numeric(38,18)` rounds anything below half of `OUTCOME_PRICE_MIN` to zero, and the
                # column's own `price > 0` then refuses the row. The provider publishes `mark: null`
                # and DexScreener publishes pools printing 2.94e-27, so both ends are reachable, and a
                # refused INSERT would take the whole turn -- ingestion included -- down with it. The
                # horizon stays due and is banked `unavailable` after the grace, like any other row
                # nothing could price.
                price = None
            if price is None and not bool(row["expired"]):
                # Still due. A horizon nothing could price yet is retried next turn rather than banked
                # as a number nobody measured.
                continue
            written.append(
                WalletOutcome(
                    delivery_key=str(row["delivery_key"]),
                    horizon=row["horizon"],
                    price=price,
                    at_ms=stamp,
                    source=source if price is not None else OUTCOME_UNAVAILABLE,
                )
            )
        if not written:
            return DeriveResult()
        stored = await self._write(
            "news_chain_tape_outcomes",
            lambda repos: sum(int(repos.news.chain_tape_record_outcome(row)) for row in written),
            errors,
        )
        if stored is FAILED:
            return DeriveResult()
        return DeriveResult(
            outcomes=sum(1 for row in written if row.price is not None),
            unavailable=sum(1 for row in written if row.price is None),
        )

    async def _price(self, token: str, errors: list[str]) -> Decimal | None:
        if self.prices is None:
            return None
        answer = await self._call(PRICE_SOURCE, lambda: self.prices.token_price(token), errors)  # type: ignore[union-attr]
        return answer if isinstance(answer, Decimal) else None

    # ------------------------------------------------------------------ provider context
    async def _bags(self, handle: str, errors: list[str]) -> tuple[Any, ...] | None:
        """The provider's open positions for one handle, or `None` when the provider did not answer.

        The distinction is the whole of B2: an empty tuple is the site saying "this wallet holds none
        of anything", which is evidence, and `None` is the site saying nothing at all, which is not.
        """

        answer = await self._call(SITE_SOURCE, lambda: self.site.bags(handle), errors)
        return None if answer is FAILED else tuple(answer or ())

    async def _marks(self, errors: list[str]) -> Mapping[str, Any]:
        answer = await self._call(SITE_SOURCE, self.site.marks, errors)
        return {} if answer is FAILED or answer is None else answer

    # ------------------------------------------------------------------ storage
    async def _commit(self, plan: _Plan, errors: list[str]) -> bool:
        """One transaction: every check this pass made, and every Item and fact row it decided on.

        The Item and its typed fact are written by `admit_market_item`, which is the same function the
        provider's four market kinds go through. There is no second admission path, and there is no
        window in which a `news_items` row exists without the observation it stands for.
        """

        def _write(repos: Any) -> int:
            for check in plan.checks:
                repos.news.chain_tape_record_check(check)
            opened = 0
            for event in plan.events:
                result = admit_market_item(
                    repos,
                    prepare_wallet_observation(event),
                    # Live: this is a to-do for the notification loop. Anything else would make a card
                    # that the reader is meant to receive `historical` on arrival (#553 §4.1.5).
                    ingest_mode="live",
                    trace_id=f"chain-tape:{event.kind}",
                    now_ms=event.received_at_ms,
                )
                opened += int(bool(result.fact_written))
            return opened

        written = await self._write("news_chain_tape_wallet_cards", _write, errors)
        return written is not FAILED

    async def _call(self, source: NewsExternalDataSource, call: Callable[[], Any], errors: list[str]) -> Any:
        """One bounded provider attempt, measured. A failure is this line's answer, never the turn's."""

        started = time.perf_counter()
        try:
            answer = await call()
        except Exception as exc:  # provider failures are expected; a card loses a line, not its send
            code = getattr(exc, "code", None) or type(exc).__name__
            errors.append(f"{source}:{code}")
            if self.telemetry is not None:
                self.telemetry.record_external_data_provider_call(
                    CHAIN_TAPE_NAME, source, "error", time.perf_counter() - started
                )
            return FAILED
        if self.telemetry is not None:
            self.telemetry.record_external_data_provider_call(
                CHAIN_TAPE_NAME, source, "success", time.perf_counter() - started
            )
        return answer


# --- building the two observations ----------------------------------------------------------------


def _exit_event(
    fill: ClassifiedFill,
    *,
    member: RosterMember,
    card: ExitCard,
    mark: Decimal | None,
    entry_price: Decimal | None,
    crowding_item_id: str | None,
) -> WalletEvent:
    symbol = fill.token_symbol or fill.token[:10]
    action = "清仓" if card.closed else f"减仓 {card.ratio_bps / 100:.0f}%"
    evidence: dict[str, Any] = {
        "fill": {"chain_id": fill.chain_id, "tx_hash": fill.tx_hash, "log_index": fill.log_index},
        "basis": card.basis,
        "cascade_wallets": card.cascade_wallets,
        "cascade_usd": str(card.cascade_usd),
        "balance_before_raw": str(card.balance_before_raw),
        "roster_version": fill.roster_version,
    }
    if crowding_item_id:
        # The reader who was told several wallets were piling into this token is the reader who should
        # be told the lead just got out. The link is stored, and the card prints it.
        evidence["crowding_item_id"] = crowding_item_id
    event = WalletEvent(
        item_id="",
        kind="exit",
        chain_id=fill.chain_id,
        wallet=fill.wallet,
        handle=member.handle,
        followers=member.followers,
        token=fill.token,
        token_symbol=fill.token_symbol,
        token_decimals=fill.token_decimals,
        roster_version=fill.roster_version,
        window_from_ms=fill.event_at_ms,
        window_to_ms=fill.event_at_ms,
        segment_key=card.segment_key,
        event_at_ms=fill.event_at_ms,
        received_at_ms=fill.received_at_ms,
        title=f"{member.handle or fill.wallet[:10]} {action} {symbol}",
        ratio_bps=card.ratio_bps,
        basis=card.basis,
        quantity_raw=card.quantity_raw,
        balance_before_raw=card.balance_before_raw,
        usd=fill.usd,
        position_usd=card.position_usd,
        entry_price=entry_price,
        mark_price=mark,
        peer_wallets=card.cascade_wallets,
        peer_usd=card.cascade_usd,
        tx_hash=fill.tx_hash,
        block_number=fill.block_number,
        closed=card.closed,
        evidence=evidence,
    )
    return _identified(event)


def _crowding_event(
    fill: ClassifiedFill,
    *,
    card: CrowdingCard,
    lead: RosterMember | None,
    roster_version: int,
    followers: int,
    liquidity: Decimal | None,
) -> WalletEvent:
    symbol = fill.token_symbol or fill.token[:10]
    event = WalletEvent(
        item_id="",
        kind="crowding",
        chain_id=fill.chain_id,
        wallet=card.lead.wallet,
        handle="" if lead is None else lead.handle,
        followers=followers,
        token=fill.token,
        token_symbol=fill.token_symbol,
        token_decimals=fill.token_decimals,
        roster_version=roster_version,
        window_from_ms=card.window_from_ms,
        window_to_ms=card.window_to_ms,
        segment_key=str(card.window_from_ms),
        event_at_ms=card.window_to_ms,
        received_at_ms=fill.received_at_ms,
        title=f"{len(card.buyers)} 个名单地址买入 {symbol}",
        tone="late" if card.late else "",
        usd=card.total_usd,
        entry_price=card.lead.price,
        peer_wallets=len(card.buyers),
        peer_usd=card.total_usd,
        premium_bps=card.premium_bps,
        liquidity_usd=liquidity,
        evidence={
            "buyers": [
                {"wallet": buyer.wallet, "first_at_ms": buyer.first_at_ms, "usd": str(buyer.usd)}
                for buyer in card.buyers
            ],
            "lead": card.lead.wallet,
            "roster_version": roster_version,
        },
    )
    return _identified(event)


def _identified(event: WalletEvent) -> WalletEvent:
    """Stamp the Item identity the admission path will key on, from the event's own subject."""

    return replace(event, item_id=wallet_item_id(event))


# --- small conversions ------------------------------------------------------------------------------


def _raw_amount(amount: Any, decimals: int | None) -> int | None:
    """A human quantity as the token's own raw integer, or `None` when it cannot be scaled exactly."""

    value = tape_decimal(amount)
    if value is None or decimals is None or value < 0:
        return None
    return int(value * (Decimal(10) ** int(decimals)))


def _position_value(balance_raw: int, decimals: int | None, price: Decimal | None) -> Decimal | None:
    if decimals is None or price is None or balance_raw <= 0 or price <= 0:
        return None
    return (Decimal(int(balance_raw)) / (Decimal(10) ** int(decimals))) * price


def _implied_price(fill: ClassifiedFill) -> Decimal | None:
    """The price this very trade printed: its dollars over its quantity. No feed, no staleness."""

    if fill.usd is None or fill.token_decimals is None or fill.amount_raw <= 0:
        return None
    quantity = Decimal(int(fill.amount_raw)) / (Decimal(10) ** int(fill.token_decimals))
    return None if quantity <= 0 else Decimal(fill.usd) / quantity


def _mark(row: Any) -> Decimal | None:
    return None if row is None else tape_decimal(getattr(row, "mark", None))


def _liquidity(row: Any) -> Decimal | None:
    return None if row is None else tape_decimal(getattr(row, "liquidity", None))


__all__ = [
    "OUTCOMES_PER_TURN_MAX",
    "SITE_PRICE_SOURCE",
    "BalancePort",
    "DeriveResult",
    "PricePort",
    "SiteContextPort",
    "WalletCardDeriver",
]
