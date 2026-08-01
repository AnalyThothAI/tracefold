from tracefold.market.pricing.event_anchor_backfill_worker import _terminal_reason


def test_exhausted_lease_uses_canonical_enriched_event_reason() -> None:
    assert (
        _terminal_reason(
            {
                "status": "failed",
                "last_reason": "backfill_expired",
            }
        )
        == "backfill_expired"
    )


def test_provider_terminal_reason_is_preserved() -> None:
    assert _terminal_reason({"status": "failed", "last_reason": "provider_timeout"}) == "provider_timeout"
