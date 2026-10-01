"""One process-wide provider entry for editorial sends, market sends and receipt edits."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..models import ReaderDeliveryPresentation
from ..reader_card import ReaderCard
from ..telemetry import NewsWorkSemantics

_INITIAL_SEND_TIMEOUT_SECONDS = 8.0
_DELIVERY_EDIT_TIMEOUT_SECONDS = 8.0


class NewsPushSender(Protocol):
    """Synchronous provider boundary executed by the finite-operation runner."""

    def prepare(self) -> None: ...

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


@runtime_checkable
class EditableNewsPushSender(Protocol):
    """Provider capability for replacing one already-receipted reader message in place."""

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...


class InitialSendEntry:
    """One fair, paced provider boundary shared by all News outbound owners.

    Editorial reserves ``slot`` through preflight, durable begin, send and settlement.
    Market sends and receipt edits reserve it around their provider call; each caller owns
    its own persistent intent and receipt. There is one interval and no second edit pacer.
    """

    # Transports caller-owned durable intents; this collaborator creates no independent ledger.
    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        sender: NewsPushSender | None,
        finite_operations: Any,
        min_interval_seconds: float,
        timeout_seconds: float = _INITIAL_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._sender = sender
        self._finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._lock = asyncio.Lock()
        self._last_send_at = 0.0

    @property
    def available(self) -> bool:
        """Whether a sender was configured at all. Not a health check -- composition already decided."""

        return self._sender is not None

    @property
    def editable(self) -> bool:
        return isinstance(self._sender, EditableNewsPushSender)

    async def prepare(self) -> None:
        """Read-only target validation, before a caller records a durable sending intent."""
        if self._sender is None:
            raise RuntimeError("news_delivery_sender_unavailable")
        await self._finite.run("news_delivery_prepare", self._sender.prepare, timeout_seconds=self._timeout_seconds)

    async def close(self) -> None:
        """Called by the Workers root after all send and enrichment owners have drained."""
        if self._sender is not None:
            with contextlib.suppress(Exception):
                await self._finite.run(
                    "news_delivery_sender_close", self._sender.close, timeout_seconds=5.0, allow_shutdown=True
                )

    async def send_prepared_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
        operation: str = "news_delivery_send",
        prepare: bool = True,
    ) -> Mapping[str, Any]:
        """Send one already-built card. Nothing here enriches or settles it.

        Both shapes of the same card travel together: the `ReaderCard` a channel serializes for
        itself, and the channel payload the ledger froze, which the channel that owns that wire shape
        posts verbatim. Neither is derived from the other here.

        The caller owns idempotency and the receipt, exactly as the Deliverer always has: this is the
        target check, the pacing and the provider call, and no part of either domain's decision.

        `prepare=False` is for a caller that already validated the target *earlier on purpose*. The
        Deliverer prepares inside its reserved send slot before the durable `sending` transition,
        so a bad channel settles as not_sent without a provider call. The market loop takes the
        default, because Telegram refuses `send_card` outright on an unvalidated target and a card
        that skipped the check would fail on a channel that is in fact fine.
        """

        sender = self._sender
        if sender is None:
            raise RuntimeError("news_delivery_sender_unavailable")
        async with self.slot():
            return await self.send_reserved_card(
                card,
                channel_payload=channel_payload,
                presentation=presentation,
                operation=operation,
                prepare=prepare,
            )

    async def send_reserved_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None,
        operation: str,
        prepare: bool,
    ) -> Mapping[str, Any]:
        """Called by the initial entry or by News while its send slot is held."""

        sender = self._sender
        if sender is None:
            raise RuntimeError("news_delivery_sender_unavailable")
        if prepare:
            await self.prepare()
        receipt: Mapping[str, Any] = await self._finite.run(
            operation,
            sender.send_card,
            card,
            channel_payload=dict(channel_payload),
            presentation=presentation,
            timeout_seconds=self._timeout_seconds,
        )
        return receipt

    async def send_prepared_edit(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
        operation: str = "news_delivery_edit",
        timeout_seconds: float = _DELIVERY_EDIT_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        """Replace one already-sent card in place, behind the same lock and the same interval.

        An edit is an outbound provider message like any other, and the channel counts it against the
        same per-chat rate a send is counted against. The Deliverer used to pace it separately, which
        made the operator's one interval mean two different things at once; the only thing the caller
        still owns is the durable `editing` intent it must hold before it gets here.

        `allow_shutdown` is the one asymmetry and it is the caller's contract, not this entry's: an
        edit that is still in flight when the process is asked to stop may finish, because the message
        it is replacing is already on the reader's screen either way.
        """

        sender = self._sender
        if not isinstance(sender, EditableNewsPushSender):
            raise RuntimeError("news_delivery_editable_sender_unavailable")
        async with self.slot():
            edited: Mapping[str, Any] = await self._finite.run(
                operation,
                sender.edit_card,
                dict(receipt),
                card,
                channel_payload=dict(channel_payload),
                presentation=presentation,
                timeout_seconds=timeout_seconds,
                allow_shutdown=True,
            )
            return edited

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Reserve one outbound turn, no sooner than the operator's interval.

        The stamp covers the whole held block, not just a successful call. A `prepare` that raises is
        still a provider call this process just made, and leaving the stamp stale would let the next
        caller compute `wait <= 0` -- so a turn draining its card budget against a broken target would
        hammer the preflight with no interval between attempts at all.
        """

        async with self._lock:
            try:
                wait = self.min_interval - (time.monotonic() - self._last_send_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                yield
            finally:
                self._last_send_at = time.monotonic()
