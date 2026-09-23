"""Novelty replays over the frozen #651 sequences, in clock order, through the real policy.

Each case is a read-only production export: the Event, the judgment that was persisted for it, the told
ledger the model was actually shown, and the deliveries that settled. The replay reconstructs the worker's
inputs from that evidence and calls the same `storyline_status` and `decide()` the worker calls. Nothing
here re-implements a policy condition, and no model is invoked: a judgment is either the one production
recorded or one this test controls on purpose, and which it is, is said every time.

The `seen` ledger is the sequence's own receipts rather than the whole 4 h window production measured
against. That is the honest bound of this evidence -- the wider window can only add a withhold, never
remove one -- and it is stated per test where it matters.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.support.news_judgment import scored_judgment, triage_verdict
from tests.support.news_novelty_sequences import (
    event,
    frozen_policy,
    gate_facts,
    history_row,
    judgment,
    seen_rows,
    sequence,
    sequences,
    settled_at_ms,
    storyline_key,
    told,
    triage_stamp,
)
from tracefold.news.reader_history import build_reader_history
from tracefold.news.similarity import trigram_similarity
from tracefold.news.told_context import (
    TOLD_MAX,
    TOLD_RECENCY_RESERVED,
    TOLD_RECENCY_WINDOW_MS,
    ToldLedgerSnapshot,
)
from tracefold.news.triage_rules import DEFAULT_POLICY, GateFacts, decide, storyline_status

VISA = "visa_onchain_credit"
CP = "cp_listing_three_venues"


def _steps(sequence_id: str) -> list[dict]:
    return [dict(step) for step in sequence(sequence_id)["steps"]]


def _status(case_key: str, *, delivered: list[str]):
    """The storyline status the worker would have built for this card from those receipts."""

    return storyline_status(
        storyline_key(case_key),
        told=told(case_key),
        seen=seen_rows(delivered, case_key=case_key),
    )


def test_every_sequence_step_names_the_told_entry_it_claims_to_repeat() -> None:
    """The gold target is an index into a real ledger, so the metric can check a `duplicate_of` against it.

    `expected_duplicate_of` is the event the card repeats and `told_index_of_target` is where that event
    sat in this card's own visible ledger. A pair that does not line up would let a later scorer credit a
    citation nobody could have made, so the fixture is checked against the export it was derived from.
    """

    for sequence_document in sequences():
        for step in sequence_document["steps"]:
            case_key = str(step["case"])
            assert str(step["event_id"]) == str(event(case_key)["event_id"])
            assert str(step["storyline_key"]) == storyline_key(case_key)
            index = step["told_index_of_target"]
            target = step["expected_duplicate_of"]
            if index is None:
                assert target is None, case_key
                continue
            entry = told(case_key)[int(index)]
            assert entry["event_id"] == target, case_key
            assert entry["i"] == int(index), case_key


def test_the_replayed_knobs_are_the_shipped_defaults() -> None:
    """#651 §6.3 changes a rule, not a number: `TOLD_MAX`, the windows and `similarity_max` do not move.

    Every frozen arm in these sequences ran the default knobs, so the replay below is the current policy
    over the same values production used rather than the current policy over a tuned one.
    """

    for sequence_document in sequences():
        for step in sequence_document["steps"]:
            assert frozen_policy(str(step["case"])) == DEFAULT_POLICY, step["case"]


def test_the_visa_release_is_dropped_once_it_is_called_a_restatement() -> None:
    """The #630 counterexample, as a controlled label over the frozen evidence.

    `ec2e5a29` and `727ffc0b` are one Visa onchain-credit release carried by two outlets 102 minutes
    apart; the first was delivered and sits at told i=6 of the second. The controlled judgment is the gold
    label -- `restatement` citing 6 -- carrying the direction the first card did *not*: policy v13 read
    that flip as proof of a reversal and let the card through, which is how the reader received the same
    release twice. The similarity check cannot save it either: these two headlines score 0.16.
    """

    first, second = _steps(VISA)
    case_key = str(second["case"])
    entry = told(case_key)[6]
    assert entry["event_id"] == str(first["event_id"]) and entry["direction"] == "bullish"
    assert trigram_similarity(entry["headline_zh"], event(case_key)["comparison_title"]) < 0.25

    status = _status(case_key, delivered=[str(first["case"])])
    controlled = judgment(case_key, novelty="restatement", restates=6, direction="bearish")
    policy = frozen_policy(case_key)
    result = decide(controlled, gate_facts(case_key), status, policy=policy, now_ms=triage_stamp(case_key))

    assert (result.final, result.override_rule, result.throttled_by) == ("drop", "restatement", None)
    assert result.final == second["controlled"]["expected_final_decision"]
    assert result.override_rule == second["controlled"]["expected_override_rule"]

    # And the drop is this guard rather than an accident of another rule: switching the guard off is the
    # only edit, and the same card reaches the reader.
    without_guard = decide(
        controlled,
        gate_facts(case_key),
        status,
        policy=replace(policy, restatement_drop=False),
        now_ms=triage_stamp(case_key),
    )
    assert without_guard.final == "push"


def test_the_recorded_visa_judgment_still_pushes_because_it_claims_a_progression() -> None:
    """The other half of the same card, and the boundary of what this change can prove.

    What actually shipped was `progression`/`restates=-1`, and policy has no standing to contradict a
    novelty label -- so the replayed action is `push`, exactly as recorded. Calling this release a
    progression is the seed's error, and the seed half of #651 §6.3 is NOT VERIFIED here: proving the
    model now labels it a restatement needs a model call, which this suite does not make.

    The rule name is the one thing that moved: production named this push `trade_relevance_realtime`,
    and v16 names the observation it pushed on. `product_service_change` is not one of the four escalate
    families, so a state change in it is an ordinary card.
    """

    first, second = _steps(VISA)
    case_key = str(second["case"])
    status = _status(case_key, delivered=[str(first["case"])])
    result = decide(
        judgment(case_key),
        gate_facts(case_key),
        status,
        policy=frozen_policy(case_key),
        now_ms=triage_stamp(case_key),
    )

    assert result.final == second["recorded"]["final_decision"] == "push"
    assert result.override_rule == "fact_kind_state_change"
    assert second["recorded"]["novelty"] == "progression"
    assert second["expected_duplicate_of"] == str(first["event_id"])


def test_the_cp_listing_chain_drops_the_channel_repeat_and_keeps_the_new_venue() -> None:
    """One notice, the same notice on another channel, then a different venue.

    The middle card is the repeat and the third is a real progression: Bithumb listing CP is not Upbit
    listing CP. The three cards share `asset:CP`, and a dropped card is not a receipt, so the third is
    measured against the one CP card that was ever delivered.
    """

    upbit, cross_channel, bithumb = _steps(CP)
    assert settled_at_ms(str(cross_channel["case"])) is None

    for step, delivered, expected in (
        (upbit, [], ("push", "listing_deterministic", None)),
        (cross_channel, [str(upbit["case"])], ("drop", "restatement", None)),
        (bithumb, [str(upbit["case"])], ("push", "listing_deterministic", None)),
    ):
        case_key = str(step["case"])
        status = _status(case_key, delivered=delivered)
        result = decide(
            judgment(case_key),
            gate_facts(case_key),
            status,
            policy=frozen_policy(case_key),
            now_ms=triage_stamp(case_key),
        )
        assert (result.final, result.override_rule, result.throttled_by) == expected, case_key
        assert result.final == step["recorded"]["final_decision"], case_key
        # The receipt ledger the third card was measured against carries the delivered notice and not the
        # dropped one.
        assert list(status.seen_event_ids) == [str(upbit["event_id"])] * len(delivered)

    assert cross_channel["told_index_of_target"] == 6
    assert told(str(cross_channel["case"]))[6]["event_id"] == str(upbit["event_id"])
    assert bithumb["expected_duplicate_of"] is None


def test_a_dense_instrument_history_can_no_longer_hide_the_card_this_one_repeats() -> None:
    """#675 §2 A1, over the frozen CP notice rather than over synthetic rows.

    The restatement guard can only fire on a ledger entry the model was shown, so the whole chain above
    rests on the delivered Upbit notice being selected at all. It is 25 minutes old and its storyline key
    is the same, which is why it was selected in production -- but the audit found 21 of 37 duplicate pairs
    where the earlier card had *no* tier against the candidate and was evicted by 48 h of same-instrument
    traffic. Here that pressure is applied deliberately: a dense pool of older cards about the same
    instrument, each of which outranks a keyless recent card on the old selector.

    The point is membership, not order. The notice is selected, so `restates` can name it, and the drop the
    chain above asserts remains reachable.
    """

    upbit, cross_channel, _ = _steps(CP)
    case_key = str(cross_channel["case"])
    current = event(case_key)
    now_ms = triage_stamp(case_key)
    notice = history_row(str(upbit["case"]))
    assert now_ms - notice["at_ms"] < TOLD_RECENCY_WINDOW_MS

    # Sixteen older cards the targeted retrieval matched on an exact fact fingerprint: the top tier, which
    # is uncapped, so on rank alone they fill the whole ledger and the 25-minute notice falls out of it.
    crowd = [
        {
            **notice,
            "event_id": f"crowd{index:02d}",
            "at_ms": now_ms - (4 + index) * 3_600_000,
            "comparison_fingerprint": f"crowd{index:02d}",
            "history_scope": "targeted",
            "retrieval_reason": "exact_fingerprint",
        }
        for index in range(TOLD_MAX)
    ]

    def _select(rows: list[dict]) -> list[str]:
        snapshot = ToldLedgerSnapshot.select(
            rows,
            now_ms=now_ms,
            storyline_key=storyline_key(case_key),
            symbols=[str(value) for value in current["grounded_assets"] or ()],
            comparison_title=str(current["comparison_title"] or ""),
            exclude_event_id=str(cross_channel["event_id"]),
        )
        assert len(snapshot.entries) == TOLD_MAX
        return [entry.event_id for entry in snapshot.entries]

    assert str(upbit["event_id"]) in _select([*crowd, notice])
    # The reservation is what holds it: age the same card past the window and the crowd takes every slot.
    aged = {**notice, "at_ms": now_ms - TOLD_RECENCY_WINDOW_MS - 60_000}
    assert str(upbit["event_id"]) not in _select([*crowd, aged])
    # And the crowd still owns the other ten slots: the reservation is a floor of six, not a takeover.
    burst = [{**notice, "event_id": f"fresh{index:02d}", "at_ms": now_ms - (index + 1) * 60_000} for index in range(10)]
    kept = _select([*crowd, *burst])
    assert sum(1 for event_id in kept if event_id.startswith("crowd")) == TOLD_MAX - TOLD_RECENCY_RESERVED


def test_a_reversal_reaches_as_a_progression_and_drops_as_a_restatement() -> None:
    """The split the change rests on, over one pair of frozen texts.

    The cross-channel CP card scores 0.79 against the delivered Upbit card -- far above `similarity_max`,
    so the same-fact check is what decides it. Called a `progression` with the opposite direction it is
    exempt and reaches: `_seen_flip` is untouched, and a real reversal arrives wearing that label. Called
    a `restatement` of the told entry it actually repeats, it drops before the same-fact check is reached.
    The told entry here is `neutral`, so this half is a P2P for the label path rather than for the flip;
    the flip is proven on the Visa pair above, whose told entry is directional.

    Two things are controlled. The receipt's `direction`: the Upbit card was judged `neutral`, and a
    neutral row cannot be contradicted, so this holds it `bullish` to make the reversal a real one. And
    the notice's `source_authority`, held at `unknown`: the same-fact check is a rule about an ordinary
    push, and a `market_access` state change the registry can name is an `escalate` under v16, exempt
    from the check for its own reason. Held unknown and carried by one text, the card is the ordinary
    push the exemption is being measured on.
    """

    upbit, cross_channel, _bithumb = _steps(CP)
    case_key = str(cross_channel["case"])
    directional_receipt = {**history_row(str(upbit["case"])), "direction": "bullish"}
    snapshot = build_reader_history(
        [directional_receipt],
        now_ms=triage_stamp(case_key),
        dedupe_family=str(event(case_key)["dedupe_family"] or "general"),
        comparison_fingerprint=str(event(case_key)["comparison_fingerprint"] or ""),
        canonical_assets=[str(value) for value in event(case_key)["grounded_assets"] or ()],
        comparison_title=str(event(case_key)["comparison_title"] or ""),
        include_targeted=False,
    )
    status = storyline_status(
        storyline_key(case_key),
        told=told(case_key),
        seen=[row.as_told_row() for row in snapshot.recent_seen_rows],
    )
    policy = frozen_policy(case_key)
    facts = gate_facts(case_key)

    reversal = decide(
        judgment(case_key, novelty="progression", restates=-1, direction="bearish", source_authority="unknown"),
        facts,
        status,
        policy=policy,
        now_ms=triage_stamp(case_key),
    )
    assert reversal.seen_similarity is not None and reversal.seen_similarity >= policy.similarity_max
    assert (reversal.final, reversal.throttled_by) == ("push", None)

    repeat = decide(
        judgment(case_key, novelty="restatement", restates=6, direction="bearish", source_authority="unknown"),
        facts,
        status,
        policy=policy,
        now_ms=triage_stamp(case_key),
    )
    assert (repeat.final, repeat.override_rule) == ("drop", "restatement")


def test_a_third_cp_card_inside_the_hour_is_not_withheld_by_how_many_came_before() -> None:
    """Policy v17 over frozen production receipts: the #504 D2 per-storyline budget is gone.

    Two CP cards were delivered on `asset:CP` inside one hour, the newest directional one `bullish`. Under
    v12-v16 an ordinary third card on the key was withheld as `storyline:asset:CP:budget` unless it reversed
    that card; the owner withdrew the rule on 2026-09-23, and under v17 neither the count nor the direction
    decides anything. `similarity_max` is set to zero, the documented way to switch the same-fact check off,
    so the only rule this test could be measuring is the deleted one. The notice's `source_authority` is
    held at `unknown` so the card is an ordinary push rather than the `market_access` escalate v16 grants a
    source the registry can name.
    """

    upbit, cross_channel, bithumb = _steps(CP)
    case_key = str(cross_channel["case"])
    policy = replace(frozen_policy(case_key), similarity_max=0.0)
    now_ms = int(bithumb["settled_at_ms"]) + 60_000
    receipts = [history_row(str(upbit["case"])), history_row(str(bithumb["case"]))]
    assert {row["storyline_key"] for row in receipts} == {"asset:CP"}
    assert [row["direction"] for row in receipts] == ["neutral", "bullish"]
    assert now_ms - min(int(row["at_ms"]) for row in receipts) < 3_600_000

    snapshot = build_reader_history(
        receipts,
        now_ms=now_ms,
        dedupe_family=str(event(case_key)["dedupe_family"] or "general"),
        comparison_fingerprint=str(event(case_key)["comparison_fingerprint"] or ""),
        canonical_assets=[str(value) for value in event(case_key)["grounded_assets"] or ()],
        comparison_title=str(event(case_key)["comparison_title"] or ""),
        include_targeted=False,
    )
    status = storyline_status(
        storyline_key(case_key),
        told=told(case_key),
        seen=[row.as_told_row() for row in snapshot.recent_seen_rows],
    )
    assert len(status.seen_event_ids) == 2
    facts = gate_facts(case_key)

    for direction in ("neutral", "bullish", "bearish"):
        result = decide(
            judgment(case_key, novelty="progression", restates=-1, direction=direction, source_authority="unknown"),
            facts,
            status,
            policy=policy,
            now_ms=now_ms,
        )
        assert (result.final, result.throttled_by) == ("push", None), direction


_MACRO_PAIRS = (
    # Two countries' releases of the same indicator. This pair scores 0.27 on character trigrams -- above
    # `similarity_max`, so had the earlier print been inside the 4 h receipt ledger the deterministic check
    # would have withheld the second one. That the receipt ledger and the wider `told` evidence are
    # separate sets is what keeps that counterfactual out of the mechanical layer (`ReaderHistorySnapshot`).
    ("英国8月制造业PMI终值51.7", "美国8月标普全球制造业PMI终值53.9"),
    # One trade story, two different traded quantities. 0.20.
    ("中国8月原油进口同比增4.3%", "中国8月成品油出口同比降11%"),
    # One series, two statistical periods. 0.19.
    ("美国二季度GDP终值上修至2.6%", "美国三季度GDP初值1.8%"),
)


@pytest.mark.parametrize(("told_headline", "candidate_headline"), _MACRO_PAIRS)
def test_different_economic_events_in_one_storyline_reach_the_reader_as_new_facts(
    told_headline: str, candidate_headline: str
) -> None:
    """Policy cannot tell these apart, so the seed has to.

    Each pair sits under one macro storyline and shares most of its wording, and the only thing that
    decides whether the second reaches the reader is the novelty label the model wrote. With `new_fact`
    the card is delivered; with `restatement` citing the row beside it, the same card is dropped by the
    same guard that drops a real repeat. That asymmetry is why "two different economic events are not one
    fact because one storyline covers both" is a sentence in the instruction and not a rule in the code.
    """

    now_ms = 1_788_800_000_000
    key = "topic:macro_data"
    rows = [
        {
            "i": 0,
            "event_id": "a" * 64,
            "at_ms": now_ms - 6 * 3_600_000,
            "storyline_key": key,
            "comparison_title": told_headline,
            "symbols": [],
            "direction": "neutral",
            "headline_zh": told_headline,
            "why_zh": "",
        }
    ]
    # `told` only: the earlier print is six hours old, outside the 4 h receipt ledger `decide()` measures
    # duplicates against and inside the 48 h evidence the model reads.
    status = storyline_status(key, told=rows, seen=[])
    facts = GateFacts(grounded_assets=(), watchlist_symbols=frozenset(), admission="candidate")
    verdict = triage_verdict(
        novelty="new_fact",
        restates=-1,
        assets=[],
        scope="macro",
        direction="neutral",
        # An official statistic printed at a new value, which is what every headline in the pairs is.
        fact_kind="new_quantity",
        headline_zh=candidate_headline,
        why_zh="宏观数据更新。",
    )

    new_fact = decide(
        scored_judgment(verdict),
        facts,
        status,
        policy=DEFAULT_POLICY,
        now_ms=now_ms,
    )
    assert (new_fact.final, new_fact.throttled_by) == ("push", None)

    mislabelled = decide(
        scored_judgment(verdict.model_copy(update={"novelty": "restatement", "restates": 0})),
        facts,
        status,
        policy=DEFAULT_POLICY,
        now_ms=now_ms,
    )
    assert (mislabelled.final, mislabelled.override_rule) == ("drop", "restatement")
