"""At-most-once reader-card delivery stage."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol

from ..bus import Q_DELIVER, BusMessage, DeferError, PermanentError, TransientError, now_ms
from ..delivery import reader_assets, reader_market_movements, reader_trade_targets, render_first_card
from ..market_review.pricing import REACTION_METRIC_VERSION
from ..models import ReaderDeliveryPresentation
from ..oi_signals import DEFAULT_OI_POLICY, OiPolicy, program_sha256
from ..oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from ..oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from ..telemetry import NewsWorkSemantics
from .runtime import NewsDatabasePort

# The quote read gets its own short session. A price is display-only and must
# never delay, retry, or suppress a delivery; every failure degrades to no
# market line while the card proceeds normally (#113).
_QUOTE_READ_TIMEOUT_SECONDS = 1.5


class NewsPushSender(Protocol):
    """Synchronous provider boundary executed by the finite-operation runner."""

    def prepare(self) -> None: ...

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class DelivererConsumer:
    """SAC consumer: one provider attempt per (event, kind); crash between send and ack never resends."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        sender: NewsPushSender | None,
        finite_operations: Any,
        min_interval_seconds: float,
        oi_policy: OiPolicy = DEFAULT_OI_POLICY,
    ) -> None:
        self.bus = bus
        self.db = db
        self.sender = sender
        self.finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        self._oi_program_sha256 = program_sha256(oi_policy)
        self._last_send_at = 0.0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_delivery_reconcile", lambda repos: repos.news.terminalize_interrupted_deliveries(now_ms=now_ms())
            )
        await self.bus.consume(Q_DELIVER, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        kind = "first"  # one Event, one card; there is no follow-up lane
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        bundle = await self.db.read("news_delivery_load", lambda repos: self._load(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_delivery_inputs_missing")
        card, triage_row, oi_signal, _admission, event_kind, timing = bundle
        # A delivery message can outlive the source-contract migration that held its Event. Immutable
        # evidence and historical verdicts remain audit facts; current PostgreSQL routing still wins before
        # a delivery ledger row, quote read, or external send is attempted.
        if event_kind == "unsupported_market":
            return
        tv = dict(triage_row.get("verdict") or {})
        if triage_row["final_decision"] not in {"push", "escalate"}:
            return
        if self.sender is None:
            await self._settle_direct(event_id, kind, "delivery_unavailable", stamp)
            return
        try:
            await self.finite.run(
                "news_delivery_prepare",
                self.sender.prepare,
                timeout_seconds=8.0,
            )
        except Exception as exc:
            prepare_error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
            await self._settle_direct(event_id, kind, prepare_error_code, stamp)
            return
        # Only query a quote after every policy return above. A quote failure
        # never changes the delivery decision.
        shown = reader_assets(
            event_kind=event_kind,
            verdict=tv,
            grounded_assets=list(card.get("grounded_assets") or []),
            program_version=str(triage_row.get("program_version") or ""),
            verdict_program_sha256=str(triage_row.get("program_sha256") or ""),
            expected_program_sha256=self._oi_program_sha256,
            oi_signal=oi_signal,
        )
        quotes, reactions = await self._market_data(event_id, shown, stamp)
        card_payload = render_first_card(
            event=card,
            verdict=tv,
            decision=str(triage_row["final_decision"]),
            grounded_assets=list(card.get("grounded_assets") or []),
            assets=shown,
            degraded=bool(triage_row.get("degraded")),
            quotes=quotes,
        )
        news_at_ms = int(timing["news_at_ms"]) if timing and timing.get("news_at_ms") is not None else None
        reaction_anchor_at_ms = (
            int(timing["reaction_anchor_at_ms"]) if timing and timing.get("reaction_anchor_at_ms") is not None else None
        )
        observed_at_ms = int(timing["observed_at_ms"]) if timing and timing.get("observed_at_ms") is not None else None
        presentation = ReaderDeliveryPresentation(
            trade_targets=reader_trade_targets(quotes),
            market_movements=reader_market_movements(
                shown,
                quotes,
                reactions,
                news_at_ms=news_at_ms,
                now_ms=stamp,
                reaction_anchor_at_ms=reaction_anchor_at_ms,
            ),
            news_at_ms=news_at_ms,
            observed_at_ms=observed_at_ms,
        )
        state = await self.db.tx(
            "news_delivery_begin",
            lambda repos: repos.news.begin_delivery(event_id=event_id, kind=kind, card=card_payload, now_ms=stamp),
        )
        if state != "new":
            if state == "sending":
                await self.db.tx(
                    "news_delivery_ambiguous",
                    lambda repos: repos.news.settle_delivery(
                        event_id=event_id,
                        kind=kind,
                        state="terminal",
                        receipt=None,
                        error_code="ambiguous_after_crash",
                        now_ms=now_ms(),
                    ),
                )
            return
        wait = self.min_interval - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        error_code: str | None = None
        receipt: dict[str, Any] | None = None
        try:
            result = await self.finite.run(
                "news_delivery_send",
                self.sender.send_card,
                card_payload,
                presentation=presentation,
                timeout_seconds=8.0,
            )
            receipt = dict(result)
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
        finally:
            self._last_send_at = time.monotonic()
        settled_state = "sent" if error_code is None else "terminal"
        try:
            await self.db.tx(
                "news_delivery_settle",
                lambda repos: repos.news.settle_delivery(
                    event_id=event_id,
                    kind=kind,
                    state=settled_state,
                    receipt=receipt,
                    error_code=error_code,
                    now_ms=now_ms(),
                ),
            )
        except (TransientError, DeferError) as exc:
            raise RuntimeError("news_delivery_settlement_unavailable") from exc

    async def _settle_direct(self, event_id: str, kind: str, error_code: str, stamp: int) -> None:
        def _fn(repos: Any) -> None:
            state = repos.news.begin_delivery(event_id=event_id, kind=kind, card={}, now_ms=stamp)
            if state == "new":
                repos.news.settle_delivery(
                    event_id=event_id, kind=kind, state="terminal", receipt=None, error_code=error_code, now_ms=stamp
                )

        await self.db.tx("news_delivery_settle_direct", _fn)

    async def _market_data(self, event_id: str, shown: Sequence[str], stamp: int) -> tuple[list[Any], list[Any]]:
        """Fresh prices for exactly the assets rendered on the card.

        The caller passes the same code-verified asset list to the renderer, so
        the facts and quote lines cannot describe different symbols. Resolution
        remains owned by PriceRepository. Every price-plane failure returns an
        empty display value and leaves the already-made send decision untouched.
        """

        if not shown:
            return [], []
        try:
            rows = await self.db.read(
                "news_delivery_quotes",
                lambda repos: (
                    repos.price.quotes_for_symbols(shown, now_ms=stamp),
                    repos.price.event_reactions(event_id, metric_version=REACTION_METRIC_VERSION),
                ),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:  # price is display-only; all failures degrade to no line
            return [], []
        quotes, reactions = rows
        return list(quotes or []), list(reactions or [])

    def _load(self, repos: Any, event_id: str, stamp: int) -> tuple[Any, ...] | None:
        del stamp
        card = repos.news.event_card(event_id)
        routing = repos.news.event_admission(event_id)
        timing = repos.news.event_delivery_timing(event_id)
        if card is None or routing is None:
            return None
        admission = str(routing.get("admission") or "")
        event_kind = str(routing.get("event_kind") or "")
        if event_kind == "unsupported_market":
            return card, None, None, admission, event_kind, timing
        triage = repos.news.latest_verdict(event_id=event_id, stage="triage")
        if triage is None:
            return None
        oi_signal = None
        if (
            event_kind == "oi"
            and str(triage.get("program_version") or "") == OI_PROGRAM_VERSION
            and str(triage.get("program_sha256") or "") == self._oi_program_sha256
        ):
            oi_signal = repos.news.oi_signal(event_id=event_id, metric_version=OI_METRIC_VERSION)
        return card, triage, oi_signal, admission, event_kind, timing

    async def close(self) -> None:
        if self.sender is not None:
            with contextlib.suppress(Exception):
                await self.finite.run(
                    "news_delivery_sender_close", self.sender.close, timeout_seconds=5.0, allow_shutdown=True
                )
