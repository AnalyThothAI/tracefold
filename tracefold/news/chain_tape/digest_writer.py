"""When the wallet digest is due, and where it goes (#572 §5.4).

The impure half, in the same relation to `digest` that `derive` is in to `rules`: this module owns the
clock, the database checkouts, the one model call and the write. It runs inside `ChainTapeLoop`'s own
turn -- there is no second task and no second scheduler -- and like the card rules it can never fault
the tape: a failed read, a refused write, an unreachable model and a provider that will not answer all
end in either "no digest this turn" or "the template's wording".

The model call is deliberately between two checkouts and inside neither. The pack is read, the
connection is released, the call is made, and a short transaction writes the result -- which is what
#570's boundary 11 asks of every News task that talks to somebody else's server.

`digest` may be imported by storage; this may not, because it reaches the admission path and the
admission path reaches the concrete News repository. That is the same edge `derive` sits on, and the
composition root resolves it the same way: it imports this module directly and the loop sees a port.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from ..bus import DeferError, TransientError
from ..pipeline.admission import admit_market_item, prepare_wallet_observation, wallet_item_id
from ..telemetry import (
    NewsExternalDataProviderOutcome,
    NewsExternalDataSource,
    NewsExternalDataTelemetryPort,
)
from ..wallet_contracts import DIGEST_KIND, DigestLine, WalletEvent
from .contracts import CHAIN_TAPE_NAME, RosterSnapshot
from .digest import (
    DAY_MS,
    DIGEST_BAGS_MAX,
    DIGEST_COSTS_MAX,
    DIGEST_INTERVAL_S_DEFAULT,
    DIGEST_MAX_CALLS_PER_DAY_DEFAULT,
    DIGEST_WINDOW_MAX_MS,
    DigestBagsPort,
    DigestPack,
    DigestProgramPort,
    DigestWindowRows,
    LastDigest,
    build_pack,
    ground,
    template_lines,
    window_hours,
)

_DB_READ_TIMEOUT_SECONDS: Final = 8.0
_DB_WRITE_TIMEOUT_SECONDS: Final = 10.0

SITE_SOURCE: Final[NewsExternalDataSource] = "robinhoodtrenches"

_FAILED: Final = object()

log = logging.getLogger("tracefold.news.chain_tape")


@dataclass(frozen=True, slots=True)
class DigestResult:
    """What one digest pass did, counted so a turn can report it on the tape's own state row."""

    digests: int = 0
    lines: int = 0
    model_called: bool = False
    model_used: bool = False
    lines_kept: int = 0
    lines_dropped: int = 0


@dataclass(frozen=True, slots=True)
class _Lines:
    """The sentences this pass will send, and what reconciliation did to get there."""

    lines: tuple[DigestLine, ...]
    model_called: bool
    model_used: bool
    kept: int = 0
    dropped: int = 0


class WalletDigestWriter:
    """One digest per due window, written as an ordinary `wallet` Item the existing loop sends.

    Its own object rather than more methods on the deriver: the deriver's turn is per fill and runs
    every two seconds, and this one runs six times a day over a window. What they share is the tape's
    turn, which is where `ChainTapeLoop` calls both.
    """

    def __init__(
        self,
        *,
        db: Any,
        program: DigestProgramPort | None = None,
        bags: DigestBagsPort | None = None,
        interval_s: int = DIGEST_INTERVAL_S_DEFAULT,
        max_calls_per_day: int = DIGEST_MAX_CALLS_PER_DAY_DEFAULT,
        telemetry: NewsExternalDataTelemetryPort | None = None,
        clock: Callable[[], int],
    ) -> None:
        self.db = db
        self.program = program
        self.bags = bags
        self.interval_ms = max(60_000, int(interval_s) * 1_000)
        self.max_calls_per_day = max(0, int(max_calls_per_day))
        self.telemetry = telemetry
        self._clock = clock

    async def take_digest(self, *, roster: RosterSnapshot, errors: list[str]) -> DigestResult:
        """Write the window's digest if one is due. Never able to fail the tape's ingestion half."""

        now = self._clock()
        state = await self._read(
            "news_chain_tape_digest_state",
            lambda repos: repos.news.chain_tape_last_digest(since_ms=now - DAY_MS),
            errors,
        )
        if state is _FAILED:
            return DigestResult()
        last: LastDigest | None = state
        window_to = now
        # Due against the later of "when the last digest ended" and "when one was last attempted". The
        # second half is what stops a persistently refused write from calling the model on every 2 s turn
        # for ever: the attempt is marked before the call and the mark survives the failure, so a broken
        # write costs one attempt per interval rather than 1,800.
        settled = max(
            0 if last is None else last.window_to_ms,
            0 if last is None else last.attempted_at_ms,
        )
        if settled and window_to - settled < self.interval_ms:
            return DigestResult()
        # The *window* still starts where the last digest ended, not where the last attempt did: a
        # refused write must not lose the activity it was about.
        window_from = max(
            window_to - DIGEST_WINDOW_MAX_MS,
            last.window_to_ms if last is not None and last.window_to_ms else window_to - self.interval_ms,
        )
        rows = await self._read(
            "news_chain_tape_digest_window",
            lambda repos: repos.news.chain_tape_digest_window(from_ms=window_from, to_ms=window_to),
            errors,
        )
        if rows is _FAILED or rows.is_empty():
            # A quiet four hours is not a card. #572 §5.3 says so in one word -- 空窗跳过 -- and the
            # cost of ignoring it would be six identical "nothing happened" cards a day.
            return DigestResult()
        # Everything past here costs a provider call, possibly a model call, and a write, so this is
        # where the attempt is banked.
        await self._write(
            "news_chain_tape_digest_attempt",
            lambda repos: repos.news.chain_tape_mark_digest_attempt(now_ms=now),
            errors,
        )
        handles = {member.wallet: member.handle for member in roster.members}
        holding = await self._holding_costs(rows, handles=handles, errors=errors)
        pack = build_pack(
            rows,
            window_from_ms=window_from,
            window_to_ms=window_to,
            handles=handles,
            holding_costs=holding,
        )
        outcome = await self._lines(pack, calls_today=0 if last is None else last.model_calls_last_day)
        event = _digest_event(
            pack,
            rows=rows,
            roster=roster,
            outcome=outcome,
            now_ms=now,
        )
        written = await self._write(
            "news_chain_tape_digest",
            lambda repos: bool(
                admit_market_item(
                    repos,
                    prepare_wallet_observation(event),
                    ingest_mode="live",
                    trace_id="chain-tape:digest",
                    now_ms=now,
                ).fact_written
            ),
            errors,
        )
        if written is _FAILED or not written:
            return DigestResult()
        return DigestResult(
            digests=1,
            lines=len(outcome.lines),
            model_called=outcome.model_called,
            model_used=outcome.model_used,
            lines_kept=outcome.kept,
            lines_dropped=outcome.dropped,
        )

    async def _lines(self, pack: DigestPack, *, calls_today: int) -> _Lines:
        """The model's surviving lines when enough of them survive, and the template's otherwise.

        The call happens here, between two database checkouts and inside neither: the pack was read and
        the connection released before this runs, and the write that follows opens its own.

        Reconciliation is per line (`ground`), so one rounded figure costs its own sentence rather than
        the whole card. The template takes over only when too few lines are left to be a summary, and
        the counts are carried either way -- a run where the model answered and lost six of eight lines
        is the evidence that says the instruction needs work, and the card alone cannot show it.
        """

        if self.program is None or calls_today >= self.max_calls_per_day:
            return _Lines(template_lines(pack), model_called=False, model_used=False)
        try:
            answer = await self.program.summarize(facts_json=pack.as_json())
        except Exception:  # a digest that could not be written is a digest the template writes
            # The audited LM seam already records the call itself; what this decides is only whether
            # the reader gets the model's wording or the pack's own.
            log.warning("chain tape digest model call failed; rendering the template")
            return _Lines(template_lines(pack), model_called=True, model_used=False)
        grounded = ground(pack, tuple(answer))
        if not grounded.accepted():
            return _Lines(
                template_lines(pack),
                model_called=True,
                model_used=False,
                kept=grounded.kept,
                dropped=grounded.dropped,
            )
        return _Lines(
            grounded.lines,
            model_called=True,
            model_used=True,
            kept=grounded.kept,
            dropped=grounded.dropped,
        )

    async def _holding_costs(
        self,
        rows: DigestWindowRows,
        *,
        handles: Mapping[str, str],
        errors: list[str],
    ) -> dict[tuple[str, str], Decimal | None]:
        """剩余持仓成本, from the provider's own bag, for the busiest handles and no more.

        The provider is somebody else's small public server and this runs six times a day, so the
        number of handles asked about is bounded rather than proportional to the roster. A position
        nobody asked about carries `unknown`, which the fact says in as many words.
        """

        costs: dict[tuple[str, str], Decimal | None] = {}
        if self.bags is None:
            return costs
        wanted: list[str] = []
        for flow in rows.flows[:DIGEST_COSTS_MAX]:
            handle = handles.get(flow.wallet) or ""
            if handle and handle not in wanted:
                wanted.append(handle)
        for handle in wanted[:DIGEST_BAGS_MAX]:
            started = time.perf_counter()
            try:
                bags = tuple(await self.bags.bags(handle) or ())
            except Exception as exc:  # one unreadable handle costs one unknown, never the digest
                errors.append(f"{SITE_SOURCE}:{getattr(exc, 'code', None) or type(exc).__name__}")
                self._measure(SITE_SOURCE, "error", started)
                continue
            self._measure(SITE_SOURCE, "success", started)
            for flow in rows.flows[:DIGEST_COSTS_MAX]:
                if (handles.get(flow.wallet) or "") != handle:
                    continue
                costs[(flow.wallet, flow.token)] = _bag_cost(bags, token=flow.token, symbol=flow.token_symbol)
        return costs

    def _measure(
        self,
        source: NewsExternalDataSource,
        outcome: NewsExternalDataProviderOutcome,
        started: float,
    ) -> None:
        if self.telemetry is not None:
            self.telemetry.record_external_data_provider_call(
                CHAIN_TAPE_NAME, source, outcome, time.perf_counter() - started
            )

    async def _read(self, name: str, fn: Callable[[Any], Any], errors: list[str]) -> Any:
        try:
            return await self.db.read(name, fn, timeout_seconds=_DB_READ_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            return _FAILED
        except Exception as exc:  # deliberately everything; see `_absorb`
            return self._absorb(name, exc, errors)

    async def _write(self, name: str, fn: Callable[[Any], Any], errors: list[str]) -> Any:
        try:
            return await self.db.tx(name, fn, timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            return _FAILED
        except Exception as exc:  # deliberately everything; see `_absorb`
            return self._absorb(name, exc, errors)

    def _absorb(self, name: str, exc: BaseException, errors: list[str]) -> Any:
        """Record a digest failure the port did not translate, and end this pass rather than the tape.

        The same rule the card rules follow, and for the same reason: a single derived row PostgreSQL
        will not accept would otherwise stop the chain tape on every turn, for ever, because the window
        that produced it is still there to be read again. The reason reaches the tape's own state row
        through `errors`, the turn is `partial`, and the traceback is logged.
        """

        log.exception("chain tape digest failed: %s", name)
        errors.append(f"digest:{name}:{type(exc).__name__}")
        return _FAILED


def _bag_cost(bags: Sequence[Any], *, token: str, symbol: str | None) -> Decimal | None:
    """The provider's moving-average price for one position, by address and then by symbol."""

    for bag in bags:
        if str(getattr(bag, "token", "") or "").lower() == token:
            return _decimal(getattr(bag, "avg_price", None))
    if symbol:
        for bag in bags:
            if str(getattr(bag, "symbol", "") or "").upper() == symbol.upper():
                return _decimal(getattr(bag, "avg_price", None))
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _digest_event(
    pack: DigestPack,
    *,
    rows: DigestWindowRows,
    roster: RosterSnapshot,
    outcome: _Lines,
    now_ms: int,
) -> WalletEvent:
    """One `wallet` observation whose subject is the window rather than a wallet or a token.

    `wallet` and `token` are empty on purpose, and the schema admits that for this kind alone: a
    digest names no address, and the zero address would be a claim about one. The window's start is
    the segment key, so every digest is its own notification group and every digest is a first card.
    """

    event = WalletEvent(
        item_id="",
        kind=DIGEST_KIND,
        chain_id=int(rows.chain_id),
        wallet="",
        handle="",
        followers=0,
        token="",
        token_symbol=None,
        token_decimals=None,
        roster_version=roster.roster_version,
        window_from_ms=int(pack.window_from_ms),
        window_to_ms=int(pack.window_to_ms),
        segment_key=str(int(pack.window_from_ms)),
        event_at_ms=int(pack.window_to_ms),
        received_at_ms=int(now_ms),
        title=f"名单钱包 {window_hours(pack.window_from_ms, pack.window_to_ms)} 小时摘要",
        peer_wallets=len(rows.activity),
        evidence={
            "lines": [{"text": line.text, "cites": list(line.cites)} for line in outcome.lines],
            "facts": [{"id": fact.id, "text": fact.text} for fact in pack.facts],
            "pack_sha256": pack.sha256(),
            "model_called": outcome.model_called,
            "model_used": outcome.model_used,
            # What reconciliation did, whichever wording went out. Only the failures answer "is the
            # instruction working", and they are not recoverable from the card.
            "lines_kept": outcome.kept,
            "lines_dropped": outcome.dropped,
            "roster_version": roster.roster_version,
        },
    )
    return replace(event, item_id=wallet_item_id(event))


__all__ = ["DigestResult", "WalletDigestWriter"]
