from tracefold.news.events.identity import describe_exact_atom


def test_exact_atom_identity_normalizes_presentation_noise_but_preserves_numbers() -> None:
    variants = (
        "BREAKING: Bitcoin ETF inflows accelerate after approval https://example.com/live",
        "  bitcoin   ETF inflows accelerate after approval  ",
        "“BITCOIN ETF inflows—accelerate after approval”",
        "Ｂｉｔｃｏｉｎ ETF inflows accelerate after approval",
    )

    identities = [describe_exact_atom(title) for title in variants]

    assert len({identity.comparison_fingerprint for identity in identities}) == 1
    assert {identity.dedupe_family for identity in identities} == {"general"}
    assert {identity.duplicate_window_ms for identity in identities} == {12 * 60 * 60_000}
    assert (
        describe_exact_atom("Magnitude 6.4 earthquake strikes northern Chile").comparison_fingerprint
        != describe_exact_atom("Magnitude 6.8 earthquake strikes northern Chile").comparison_fingerprint
    )
    assert (
        describe_exact_atom("Bitcoin inflows rise 4%").comparison_fingerprint
        != describe_exact_atom("Bitcoin inflows rise 5%").comparison_fingerprint
    )
    assert (
        describe_exact_atom("Fund buys $4m Bitcoin").comparison_fingerprint
        != describe_exact_atom("Fund buys $5m Bitcoin").comparison_fingerprint
    )


def test_exact_atom_identity_uses_event_family_windows_capped_to_opennews_horizon() -> None:
    assert describe_exact_atom("BTC open interest rises").duplicate_window_ms == 2 * 60 * 60_000
    assert describe_exact_atom("Magnitude 6.4 earthquake strikes Chile").duplicate_window_ms == 6 * 60 * 60_000
    assert describe_exact_atom("Acme SEC filing reports revenue").duplicate_window_ms == 12 * 60 * 60_000
    assert describe_exact_atom("https://example.com").comparison_title == ""
